# Owner(s): ["oncall: distributed"]

import copy
import functools
import unittest
from typing import Iterable, List, Tuple, Union

import torch
import torch.distributed as dist
import torch.nn as nn
from _test_fully_shard_common import (
    check_1d_sharded_parity,
    MLP,
    patch_all_gather,
    patch_reduce_scatter,
)
from torch.distributed._composable import checkpoint, replicate
from torch.distributed._composable.fsdp import FSDP, fully_shard
from torch.distributed._tensor import DTensor, init_device_mesh
from torch.distributed.device_mesh import DeviceMesh
from torch.distributed.tensor.parallel import (
    ColwiseParallel,
    parallelize_module,
    RowwiseParallel,
)
from torch.testing._internal.common_cuda import TEST_CUDA
from torch.testing._internal.common_distributed import skip_if_lt_x_gpu
from torch.testing._internal.common_fsdp import FSDPTest, FSDPTestMultiThread
from torch.testing._internal.common_utils import get_cycles_per_ms, run_tests
from torch.testing._internal.distributed._tensor.common_dtensor import (
    ModelArgs,
    Transformer,
    TransformerBlock,
)


class TestFullyShardForwardInputs(FSDPTestMultiThread):
    @property
    def world_size(self) -> int:
        return 2

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_root_move_forward_input_to_device(self):
        device = torch.device("cuda", 0)

        class ParamlessModule(nn.Module):
            def forward(self, x: torch.Tensor, ys: Tuple[torch.Tensor, ...]):
                # Check that FSDP moved the inputs to GPU, including recursing
                # into the tuple data structure
                assert x.device == device, f"Expects {device} but got {x.device}"
                assert (
                    ys[0].device == device
                ), f"Expects {device} but got {ys[0].device}"
                assert (
                    ys[1].device == device
                ), f"Expects {device} but got {ys[1].device}"
                y = ys[0] + ys[1]
                return x + y + 1

        model = ParamlessModule()
        fully_shard(model, device=device)
        x = torch.randn((3,))
        ys = (torch.randn((3,)), torch.randn((3,)))
        self.assertEqual(x.device, torch.device("cpu"))
        self.assertEqual(ys[0].device, torch.device("cpu"))
        self.assertEqual(ys[1].device, torch.device("cpu"))
        model(x, ys)


class TestFullyShardRegisteredParams(FSDPTestMultiThread):
    @property
    def world_size(self) -> int:
        return 4

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_param_registration_after_forward(self):
        """Tests the parameter registration after forward."""
        device = torch.device("cuda", 0)
        fully_shard_fn = functools.partial(fully_shard, device=device)
        # Single FSDP group
        for reshard_after_forward in (True, False, 2):
            model = MLP(8, device)
            fully_shard_fn(
                model, reshard_after_forward=reshard_after_forward
            )  # root only
            inp = torch.randn((2, 8), device="cuda")
            self._assert_dtensor_params(model.parameters())
            model(inp)  # root does not reshard after forward
            self._assert_tensor_params(model.parameters())
            model.reshard()  # however, we can manually reshard
            self._assert_dtensor_params(model.parameters())

        # Multiple FSDP groups
        for reshard_after_forward in (True, False, 2):
            model = MLP(8, device)
            fully_shard_fn(model.in_proj, reshard_after_forward=reshard_after_forward)
            fully_shard_fn(model.out_proj, reshard_after_forward=reshard_after_forward)
            fully_shard_fn(model, reshard_after_forward=reshard_after_forward)

            self._assert_dtensor_params(model.parameters())
            model(inp)
            non_root_params = list(model.in_proj.parameters()) + list(
                model.out_proj.parameters()
            )
            root_params = list(model.parameters(recurse=False))
            if reshard_after_forward is True:
                self._assert_dtensor_params(non_root_params)
            elif reshard_after_forward is False:
                self._assert_tensor_params(non_root_params)
            else:
                # Do not register the sharded post-forward parameters
                self.assertEqual(len(non_root_params), 0)
                self.assertEqual(len(root_params), 0)
            self._assert_tensor_params(root_params)
            for module in model.modules():
                if isinstance(module, FSDP):
                    module.reshard()  # however, we can manually reshard
            self._assert_dtensor_params(model.parameters())

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_param_registration_after_backward(self):
        """Tests the parameter registration after backward."""
        device = torch.device("cuda", 0)
        fully_shard_fn = functools.partial(fully_shard, device=device)
        # Single FSDP group
        for reshard_after_forward in (True, False, 2):
            model = MLP(8, device)
            fully_shard_fn(
                model, reshard_after_forward=reshard_after_forward
            )  # root only
            inp = torch.randn((2, 8), device="cuda")
            self._assert_dtensor_params(model.parameters())
            model(inp).sum().backward()
            self._assert_dtensor_params(model.parameters())

        # Multiple FSDP groups
        for reshard_after_forward in (True, False, 2):
            model = MLP(8, device)
            fully_shard_fn(model.in_proj, reshard_after_forward=reshard_after_forward)
            fully_shard_fn(model.out_proj, reshard_after_forward=reshard_after_forward)
            fully_shard_fn(model, reshard_after_forward=reshard_after_forward)
            self._assert_dtensor_params(model.parameters())
            model(inp).sum().backward()
            self._assert_dtensor_params(model.parameters())

    def _assert_tensor_params(self, params: Iterable[nn.Parameter]):
        for param in params:
            self.assertNotIsInstance(param, DTensor)
            self.assertIsInstance(param, torch.Tensor)

    def _assert_dtensor_params(self, params: Iterable[nn.Parameter]):
        for param in params:
            self.assertIsInstance(param, DTensor)


class TestFullyShard1DTrainingCore(FSDPTest):
    @property
    def world_size(self) -> int:
        return min(8, torch.cuda.device_count())

    @skip_if_lt_x_gpu(2)
    def test_train_parity_single_group(self):
        """Tests train parity with DDP for a single FSDP group."""
        self.run_subtests(
            {
                "lin_shapes": [[(16, 15), (15, 8)], [(7, 15), (15, 3)]],
            },
            self._test_train_parity_single_group,
        )

    def _test_train_parity_single_group(self, lin_shapes: List[Tuple[int, int]]):
        torch.manual_seed(42)
        model = nn.Sequential(
            nn.Linear(*lin_shapes[0]), nn.ReLU(), nn.Linear(*lin_shapes[1])
        )
        ref_model = copy.deepcopy(model).cuda()
        replicate(ref_model, device_ids=[self.rank])
        ref_optim = torch.optim.Adam(ref_model.parameters(), lr=1e-2)
        fully_shard(model)
        optim = torch.optim.Adam(model.parameters(), lr=1e-2)
        torch.manual_seed(42 + self.rank + 1)
        inp = (torch.randn((4, lin_shapes[0][0]), device="cuda"),)
        for iter_idx in range(10):
            losses: List[torch.Tensor] = []
            for _model, _optim in ((ref_model, ref_optim), (model, optim)):
                _optim.zero_grad(set_to_none=(iter_idx % 2 == 0))
                losses.append(_model(*inp).sum())
                losses[-1].backward()
                _optim.step()
            self.assertEqual(losses[0], losses[1])

    @skip_if_lt_x_gpu(2)
    def test_train_parity_multi_group(self):
        """
        Tests train parity against DDP when using multiple parameter groups for
        communication (for communication and computation overlap plus memory
        reduction).
        """
        self.run_subtests(
            {
                "reshard_after_forward": [True, False, 2],
                "device": ["cuda"],
                "delay_after_forward": [False, True],
                "delay_before_all_gather": [False, True],
                "delay_before_reduce_scatter": [False, True],
                "delay_before_optim": [False, True],
            },
            self._test_train_parity_multi_group,
        )

    def _test_train_parity_multi_group(
        self,
        reshard_after_forward: Union[bool, int],
        device: str,
        delay_after_forward: bool,
        delay_before_all_gather: bool,
        delay_before_reduce_scatter: bool,
        delay_before_optim: bool,
    ):
        # Only test individual delays or all four delays to save test time
        if (
            delay_after_forward
            + delay_before_all_gather
            + delay_before_reduce_scatter
            + delay_before_optim
            in (2, 3)
        ):
            return
        torch.manual_seed(42)
        lin_dim = 32
        model = nn.Sequential(*[MLP(lin_dim, torch.device("cpu")) for _ in range(3)])
        ref_model = copy.deepcopy(model)
        if device == "cuda":
            replicate(ref_model.cuda(), device_ids=[self.rank])
        else:
            gloo_pg = dist.new_group(backend="gloo")
            replicate(ref_model, process_group=gloo_pg)
        ref_optim = torch.optim.Adam(ref_model.parameters(), lr=1e-2)
        fully_shard_fn = functools.partial(
            fully_shard,
            device=device,
            reshard_after_forward=reshard_after_forward,
        )
        for mlp in model:
            fully_shard_fn(mlp)
        fully_shard_fn(model)
        optim = torch.optim.Adam(model.parameters(), lr=1e-2)

        delay_in_ms = 100
        orig_all_gather = dist.all_gather_into_tensor
        orig_reduce_scatter = dist.reduce_scatter_tensor

        def delayed_all_gather(*args, **kwargs):
            if delay_before_all_gather:
                torch.cuda._sleep(int(delay_in_ms * get_cycles_per_ms()))
            return orig_all_gather(*args, **kwargs)

        def delayed_reduce_scatter(*args, **kwargs):
            if delay_before_reduce_scatter:
                torch.cuda._sleep(int(delay_in_ms * get_cycles_per_ms()))
            return orig_reduce_scatter(*args, **kwargs)

        torch.manual_seed(42 + self.rank + 1)
        with patch_all_gather(delayed_all_gather), patch_reduce_scatter(
            delayed_reduce_scatter
        ):
            for iter_idx in range(10):
                inp = torch.randn((8, lin_dim), device=torch.device(device))
                losses: List[torch.Tensor] = []
                for _model, _optim in ((ref_model, ref_optim), (model, optim)):
                    _optim.zero_grad(set_to_none=(iter_idx % 2 == 0))
                    losses.append(_model(inp).sum())
                    if _model is model and delay_after_forward:
                        torch.cuda._sleep(int(delay_in_ms * get_cycles_per_ms()))
                    losses[-1].backward()
                    if _model is model and delay_before_optim:
                        torch.cuda._sleep(int(delay_in_ms * get_cycles_per_ms()))
                    _optim.step()
                self.assertEqual(losses[0], losses[1])


class TestFullyShard1DTrainingCompose(FSDPTest):
    @property
    def world_size(self) -> int:
        # Since these tests run with a larger transformer model, they may see
        # some numeric drift with >2 GPUs
        return 2

    @skip_if_lt_x_gpu(2)
    def test_train_parity_with_activation_checkpointing(self):
        """
        Tests train parity against DDP when composing with activation
        checkpointing.
        """
        self.run_subtests(
            {
                "reshard_after_forward": [True, False, 2],
                "checkpoint_impl": ["utils", "composable"],
            },
            self._test_train_parity_with_activation_checkpointing,
        )

    def _test_train_parity_with_activation_checkpointing(
        self, reshard_after_forward: Union[bool, int], checkpoint_impl: str
    ):
        assert checkpoint_impl in ("composable", "utils")
        foreach = True
        torch.manual_seed(42)
        vocab_size = 1024
        with torch.device(torch.device("cuda")):
            model_args = ModelArgs(
                n_layers=3,
                n_heads=4,
                vocab_size=vocab_size,
                max_seq_len=64,
                dropout_p=0.1,
                checkpoint_activations=(checkpoint_impl == "utils"),
            )
            model = Transformer(model_args)
        ref_model = replicate(copy.deepcopy(model), device_ids=[self.rank])
        ref_optim = torch.optim.Adam(ref_model.parameters(), lr=1e-2, foreach=foreach)
        fully_shard_fn = functools.partial(
            fully_shard,
            reshard_after_forward=reshard_after_forward,
        )
        for module in model.modules():
            if isinstance(module, TransformerBlock):
                if checkpoint_impl == "composable":
                    checkpoint(module)
                fully_shard_fn(module)
        fully_shard_fn(model)
        optim = torch.optim.Adam(model.parameters(), lr=1e-2, foreach=foreach)

        torch.manual_seed(42 + self.rank)
        # Reuse the same input across iterations to avoid loss explosion from
        # trying to learn from random inputs
        inp = torch.randint(0, vocab_size, (3, 64), device="cuda")
        check_1d_sharded_parity(self, ref_model, model, check_grads=False)
        for iter_idx in range(10):
            losses: List[torch.Tensor] = []
            for _model in (ref_model, model):
                torch.manual_seed(iter_idx + 1)  # for dropout determinism
                losses.append(_model(inp).sum())
                losses[-1].backward()
            check_1d_sharded_parity(self, ref_model, model)
            self.assertEqual(losses[0], losses[1])
            for _optim in (ref_optim, optim):
                _optim.step()
                _optim.zero_grad(set_to_none=(iter_idx % 2 == 0))
            check_1d_sharded_parity(self, ref_model, model)


class TestFullyShardSharedParams(FSDPTest):
    @property
    def world_size(self) -> int:
        return min(4, torch.cuda.device_count())

    @skip_if_lt_x_gpu(2)
    def test_train_parity_with_shared_params(self):
        self.run_subtests(
            {
                "reshard_after_forward": [False, True],
                "use_activation_checkpointing": [False, True],
            },
            self._test_train_shared_params,
        )

    def _test_train_shared_params(
        self,
        reshard_after_forward: bool,
        use_activation_checkpointing: bool,
    ):
        torch.manual_seed(42)
        model_args = ModelArgs(n_layers=3, dropout_p=0.0, weight_tying=True)
        model = Transformer(model_args)
        ref_model = copy.deepcopy(model).cuda()
        replicate(ref_model, device_ids=[self.rank])
        ref_optim = torch.optim.Adam(ref_model.parameters(), lr=1e-2)
        for module in model.modules():
            if isinstance(module, TransformerBlock):
                if use_activation_checkpointing:
                    checkpoint(module)
                fully_shard(module, reshard_after_forward=reshard_after_forward)
        fully_shard(model, reshard_after_forward=reshard_after_forward)
        optim = torch.optim.Adam(model.parameters(), lr=1e-2)

        torch.manual_seed(42 + self.rank + 1)
        for iter_idx in range(10):
            inp = torch.randint(0, model_args.vocab_size, (2, 16), device="cuda")
            losses: List[torch.Tensor] = []
            for _model, _optim in ((ref_model, ref_optim), (model, optim)):
                _optim.zero_grad(set_to_none=(iter_idx % 2 == 0))
                losses.append(_model(inp).sum())
                losses[-1].backward()
                _optim.step()
            self.assertEqual(losses[0], losses[1])


class TestFullyShard2DTraining(FSDPTest):
    @property
    def world_size(self) -> int:
        return min(4, torch.cuda.device_count())

    @skip_if_lt_x_gpu(2)
    def test_train_parity_2d_mlp(self):
        # Prefer to test with >=4 GPUs, but for 2 GPUs, use 2-way TP
        dp_size = 2 if self.world_size > 2 else 1
        global_mesh = init_device_mesh(
            "cuda", (dp_size, self.world_size // dp_size), mesh_dim_names=("dp", "tp")
        )
        self.run_subtests(
            {
                "reshard_after_forward": [False, True],
                "use_activation_checkpointing": [False, True],
                "mlp_dim": [3, 16, 17],
            },
            functools.partial(self._test_train_parity_2d_mlp, global_mesh),
        )

    def _test_train_parity_2d_mlp(
        self,
        global_mesh: DeviceMesh,
        reshard_after_forward: bool,
        use_activation_checkpointing: bool,
        mlp_dim: int,
    ):
        dp_mesh, tp_mesh = global_mesh["dp"], global_mesh["tp"]
        dp_pg = dp_mesh.get_group()  # used for `replicate()`

        torch.manual_seed(42)
        model = nn.Sequential(
            # Use multiplier of 3 to exercise uneven case
            MLP(mlp_dim, dim_multiplier=3),
            MLP(mlp_dim),
            MLP(mlp_dim, dim_multiplier=3),
        )
        ref_model = copy.deepcopy(model).cuda()
        replicate(ref_model, device_ids=[self.rank], process_group=dp_pg)
        ref_optim = torch.optim.Adam(ref_model.parameters(), lr=1e-2)

        model = parallelize_module(
            model,
            device_mesh=tp_mesh,
            parallelize_plan={
                # Pass `use_local_output=False` to keep as DTensor to preserve
                # uneven activation dims
                "0.in_proj": ColwiseParallel(use_local_output=False),
                "0.out_proj": RowwiseParallel(use_local_output=False),
                "1.in_proj": ColwiseParallel(use_local_output=False),
                "1.out_proj": RowwiseParallel(use_local_output=False),
                "2.in_proj": ColwiseParallel(use_local_output=False),
                "2.out_proj": RowwiseParallel(),
            },
        )
        for mlp in model:
            if use_activation_checkpointing:
                checkpoint(mlp)
            fully_shard(mlp, mesh=dp_mesh, reshard_after_forward=reshard_after_forward)
        fully_shard(model, mesh=dp_mesh, reshard_after_forward=reshard_after_forward)
        optim = torch.optim.Adam(model.parameters(), lr=1e-2)

        torch.manual_seed(42 + dp_pg.rank() + 1)
        device = torch.device("cuda")
        for iter_idx in range(10):
            inp = torch.randn((8, mlp_dim), device=device)
            losses: List[torch.Tensor] = []
            for _model, _optim in ((ref_model, ref_optim), (model, optim)):
                _optim.zero_grad(set_to_none=(iter_idx % 2 == 0))
                losses.append(_model(inp).sum())
                losses[-1].backward()
                _optim.step()
            self.assertEqual(losses[0], losses[1])


if __name__ == "__main__":
    run_tests()

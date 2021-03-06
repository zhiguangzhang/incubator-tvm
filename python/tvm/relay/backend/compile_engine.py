# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=len-as-condition,no-else-return,invalid-name
"""Backend code generation engine."""
from __future__ import absolute_import

import logging
import numpy as np
import tvm
from tvm import te
from ..base import register_relay_node, Object
from ... import target as _target
from ... import autotvm
from .. import expr as _expr
from .. import op as _op
from .. import ty as _ty
from . import _backend

logger = logging.getLogger('compile_engine')


@register_relay_node
class LoweredOutput(Object):
    """Lowered output"""
    def __init__(self, outputs, implement):
        self.__init_handle_by_constructor__(
            _backend._make_LoweredOutput, outputs, implement)


@register_relay_node
class CCacheKey(Object):
    """Key in the CompileEngine.

    Parameters
    ----------
    source_func : tvm.relay.Function
        The source function.

    target : tvm.Target
        The target we want to run the function on.
    """
    def __init__(self, source_func, target):
        self.__init_handle_by_constructor__(
            _backend._make_CCacheKey, source_func, target)


@register_relay_node
class CCacheValue(Object):
    """Value in the CompileEngine, including usage statistics.
    """


def _get_cache_key(source_func, target):
    if isinstance(source_func, _expr.Function):
        if isinstance(target, str):
            target = _target.create(target)
            if not target:
                raise ValueError("Need target when source_func is a Function")
        return CCacheKey(source_func, target)
    if not isinstance(source_func, CCacheKey):
        raise TypeError("Expect source_func to be CCacheKey")
    return source_func


def get_shape(shape):
    """Convert the shape to correct dtype and vars."""
    ret = []
    for dim in shape:
        if isinstance(dim, tvm.tir.IntImm):
            val = int(dim)
            assert val <= np.iinfo(np.int32).max
            ret.append(tvm.tir.IntImm("int32", val))
        elif isinstance(dim, tvm.tir.Any):
            ret.append(te.var("any_dim", "int32"))
        else:
            ret.append(dim)
    return ret


def get_valid_implementations(op, attrs, inputs, out_type, target):
    """Get all valid implementations from the op strategy.

    Note that this function doesn't support op with symbolic input shapes.

    Parameters
    ----------
    op : relay.op.Op
        Relay operator.

    attrs : object
        The op attribute.

    inputs : List[tvm.te.Tensor]
        Input tensors to the op.

    out_type : relay.Type
        The output type.

    target : tvm.target.Target
        The target to compile the op.

    Returns
    -------
    ret : List[relay.op.OpImplementation]
        The list of all valid op implementations.
    """
    fstrategy = op.get_attr("FTVMStrategy")
    assert fstrategy is not None, "%s doesn't have FTVMStrategy registered" % op.name
    with target:
        strategy = fstrategy(attrs, inputs, out_type, target)
    analyzer = tvm.arith.Analyzer()
    ret = []
    for spec in strategy.specializations:
        if spec.condition:
            # check if all the clauses in the specialized condition are true
            flag = True
            for clause in spec.condition.clauses:
                clause = analyzer.canonical_simplify(clause)
                if isinstance(clause, tvm.tir.IntImm) and clause.value:
                    continue
                flag = False
                break
            if flag:
                for impl in spec.implementations:
                    ret.append(impl)
        else:
            for impl in spec.implementations:
                ret.append(impl)
    return ret


def select_implementation(op, attrs, inputs, out_type, target, use_autotvm=True):
    """Select the best implementation from the op strategy.

    If use_autotvm is True, it'll first try to find the best implementation
    based on AutoTVM profile results. If no AutoTVM profile result is found,
    it'll choose the implementation with highest plevel.

    If use_autotvm is False, it'll directly choose the implementation with
    highest plevel.

    Note that this function doesn't support op with symbolic input shapes.

    Parameters
    ----------
    op : relay.op.Op
        Relay operator.

    attrs : object
        The op attribute.

    inputs : List[tvm.te.Tensor]
        Input tensors to the op.

    out_type : relay.Type
        The output type.

    target : tvm.target.Target
        The target to compile the op.

    use_autotvm : bool
        Whether query AutoTVM to pick the best.

    Returns
    -------
    ret : tuple(relay.op.OpImplementation, List[tvm.te.Tensor])
        The best op implementation and the corresponding output tensors.
    """
    all_impls = get_valid_implementations(op, attrs, inputs, out_type, target)

    best_plevel_impl = None
    for impl in all_impls:
        if best_plevel_impl is None or impl.plevel > best_plevel_impl.plevel:
            best_plevel_impl = impl
    if not use_autotvm:
        outs = best_plevel_impl.compute(attrs, inputs, out_type)
        return best_plevel_impl, outs

    outputs = {}
    best_autotvm_impl = None
    best_cfg = None
    dispatch_ctx = autotvm.task.DispatchContext.current
    for impl in all_impls:
        outs = impl.compute(attrs, inputs, out_type)
        outputs[impl] = outs
        workload = autotvm.task.get_workload(outs)
        if workload is None:
            continue
        cfg = dispatch_ctx.query(target, workload)
        if cfg.is_fallback:
            # It's a fallback config
            continue
        if best_cfg is None or best_cfg.cost > cfg.cost:
            best_autotvm_impl = impl
            best_cfg = cfg
    if best_autotvm_impl:
        return best_autotvm_impl, outputs[best_autotvm_impl]
    return best_plevel_impl, outputs[best_plevel_impl]


@tvm._ffi.register_func("relay.backend.lower_call")
def lower_call(call, inputs, target):
    """Lower the call expression to op implementation and tensor outputs."""
    assert isinstance(call.op, _op.Op)
    op = call.op

    # Prepare the call_node->checked_type(). For the call node inputs, we ensure that
    # the shape is Int32. Following code ensures the same for the output as well.
    # TODO(@icemelon9): Support recursive tuple
    ret_type = call.checked_type
    if isinstance(ret_type, _ty.TensorType):
        ret_type = _ty.TensorType(get_shape(ret_type.shape), ret_type.dtype)
    elif isinstance(ret_type, _ty.TupleType):
        new_fields = []
        for field in ret_type.fields:
            if isinstance(field, _ty.TensorType):
                new_fields.append(_ty.TensorType(get_shape(field.shape), field.dtype))
            else:
                new_fields.append(field)
        ret_type = _ty.TupleType(new_fields)

    is_dyn = _ty.type_has_any(call.checked_type)
    for arg in call.args:
        is_dyn = is_dyn or _ty.type_has_any(arg.checked_type)

    # check if in the AutoTVM tracing mode, and disable if op is not in wanted list
    env = autotvm.task.TaskExtractEnv.current
    reenable_tracing = False
    if env is not None and env.tracing:
        if env.wanted_relay_ops is not None and op not in env.wanted_relay_ops:
            env.tracing = False
            reenable_tracing = True

    if not is_dyn:
        best_impl, outputs = select_implementation(
            op, call.attrs, inputs, ret_type, target)
        logger.info("Use implementation %s for op %s", best_impl.name, op.name)
    else:
        # TODO(@icemelon9): Allow tvm to generate multiple kernels for dynamic shapes.
        #   Currently, we just use the implementation with highest plevel
        best_impl, outputs = select_implementation(
            op, call.attrs, inputs, ret_type, target, use_autotvm=False)

    # re-enable AutoTVM tracing
    if reenable_tracing:
        env.tracing = True
    return LoweredOutput(outputs, best_impl)


@register_relay_node
class CompileEngine(Object):
    """CompileEngine to get lowered code.
    """
    def __init__(self):
        raise RuntimeError("Cannot construct a CompileEngine")

    def lower(self, source_func, target=None):
        """Lower a source_func to a CachedFunc.

        Parameters
        ----------
        source_func : Union[tvm.relay.Function, CCacheKey]
            The source relay function.

        target : tvm.Target
            The target platform.

        Returns
        -------
        cached_func: CachedFunc
            The result of lowering.
        """
        # pylint: disable=broad-except, import-outside-toplevel
        try:
            key = _get_cache_key(source_func, target)
            return _backend._CompileEngineLower(self, key)
        except Exception:
            import traceback
            msg = traceback.format_exc()
            msg += "Error during compile func\n"
            msg += "--------------------------\n"
            msg += source_func.astext(show_meta_data=False)
            msg += "--------------------------\n"
            raise RuntimeError(msg)

    def lower_shape_func(self, source_func, target=None):
        key = _get_cache_key(source_func, target)
        return _backend._CompileEngineLowerShapeFunc(self, key)

    def jit(self, source_func, target=None):
        """JIT a source_func to a tvm.runtime.PackedFunc.

        Parameters
        ----------
        source_func : Union[tvm.relay.Function, CCacheKey]
            The source relay function.

        target : tvm.Target
            The target platform.

        Returns
        -------
        jited_func: tvm.runtime.PackedFunc
            The result of jited function.
        """
        key = _get_cache_key(source_func, target)
        return _backend._CompileEngineJIT(self, key)

    def clear(self):
        """clear the existing cached functions"""
        _backend._CompileEngineClear(self)

    def items(self):
        """List items in the cache.

        Returns
        -------
        item_list : List[Tuple[CCacheKey, CCacheValue]]
            The list of items.
        """
        res = _backend._CompileEngineListItems(self)
        assert len(res) % 2 == 0
        return [(res[2*i], res[2*i+1]) for i in range(len(res) // 2)]

    def dump(self):
        """Return a string representation of engine dump.

        Returns
        -------
        dump : str
            The dumped string representation
        """
        items = self.items()
        res = "====================================\n"
        res += "CompilerEngine dump, %d items cached\n" % len(items)
        for k, v in items:
            res += "------------------------------------\n"
            res += "target={}\n".format(k.target)
            res += "use_count={}\n".format(v.use_count)
            res += "func_name={}\n".format(v.cached_func.func_name)
            res += k.source_func.astext() + "\n"
        res += "===================================\n"
        return res


def get():
    """Get the global compile engine.

    Returns
    -------
    engine : tvm.relay.backend.CompileEngine
        The compile engine.
    """
    return _backend._CompileEngineGlobal()

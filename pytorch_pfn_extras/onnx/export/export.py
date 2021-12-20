import dataclasses
import typing
import warnings
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, Union, cast

import onnx
import onnx.checker
import onnx.helper
import onnx.numpy_helper
import onnx.shape_inference
import torch
import torch.jit
import torch.onnx.symbolic_helper as sym_hel
import torch.onnx.symbolic_registry as sym_reg
import torch.onnx.utils as to_utils
from torch.onnx import OperatorExportTypes

TorchValueID = typing.NewType("TorchValueID", int)
ONNXValueID = typing.NewType("ONNXValueID", str)

# Alias confusing function names
torch._C.Graph.returnNode = torch._C.Graph.return_node  # type: ignore[attr-defined]
torch._C.Block.return_node = torch._C.Block.returnNode  # type: ignore[attr-defined]


def _unique_id(v: torch._C.Value) -> TorchValueID:
    return TorchValueID(v.unique())


def _tensor_to_proto(t: torch.Tensor, name: Optional[ONNXValueID] = None) -> onnx.TensorProto:
    return onnx.numpy_helper.from_array(t.detach().cpu().numpy(), name)


def _type_to_proto(t: torch._C.TensorType) -> onnx.TypeProto:
    if t.kind() == "NoneType":
        return onnx.TypeProto()

    ret: onnx.TypeProto = onnx.TypeProto()
    ret.denotation = repr(t)

    if t.kind() == "ListType":
        ret.sequence_type.elem_type.CopyFrom(_type_to_proto(cast(torch._C.TensorType, t.getElementType())))
        return ret

    if t.kind() == "IntType":
        ret.tensor_type.elem_type = onnx.TensorProto.DataType.INT64
        ret.tensor_type.shape.CopyFrom(onnx.TensorShapeProto())
        return ret

    assert t.kind() == "TensorType", f"Not Tensor type(actual: {t.kind()}): {t}"

    if t.scalarType() is None:
        ret.tensor_type.elem_type = onnx.TensorProto.DataType.UNDEFINED
    else:
        ret.tensor_type.elem_type = int(  # type: ignore
            sym_hel.cast_pytorch_to_onnx[t.scalarType()]  # type: ignore[index]
        )

    ret.tensor_type.shape.CopyFrom(onnx.TensorShapeProto())
    if t.sizes() is not None:
        for s in t.sizes():  # type: ignore
            d = ret.tensor_type.shape.dim.add()
            d.dim_value = s

    assert ret.tensor_type.HasField("shape")

    return ret


def _remove_prefix(text: str, prefix: str) -> str:
    return text[text.startswith(prefix) and len(prefix) :]


def onnx_node_doc_string(onnx_node: torch._C.Node, torch_node: torch._C.Node) -> str:
    return f"""## Symbolic node
{onnx_node}
## Original node
{torch_node}
## Scope
{torch_node.scopeName()}
## Source Range
```
{torch_node.sourceRange()}
```
"""


torch_dtype_to_onnx_data_type = {
    torch.float32: onnx.TensorProto.DataType.FLOAT,
    torch.uint8: onnx.TensorProto.DataType.UINT8,
    torch.int8: onnx.TensorProto.DataType.INT8,
    torch.int16: onnx.TensorProto.DataType.INT16,
    torch.int32: onnx.TensorProto.DataType.INT32,
    torch.int64: onnx.TensorProto.DataType.INT64,
    torch.bool: onnx.TensorProto.DataType.BOOL,
    torch.float64: onnx.TensorProto.DataType.DOUBLE,
    torch.float16: onnx.TensorProto.DataType.FLOAT16,
}


def _apply_tensor_info_to_value_info(v: onnx.ValueInfoProto, t: torch.Tensor) -> None:
    v.type.tensor_type.elem_type = torch_dtype_to_onnx_data_type[t.dtype]
    v.type.tensor_type.shape.ClearField("dim")
    for i in t.shape:
        # TODO(twata): Support dynamic_axes
        a = v.type.tensor_type.shape.dim.add()
        a.dim_value = i


@dataclasses.dataclass
class _ExporterOptions:
    opset_version: int = 12

    check_trace: bool = False
    strict_trace: bool = True
    force_outplace_trace: bool = False

    verbose: bool = False
    strip_doc_string: bool = False

    torch_constant_prop: bool = True

    enable_onnx_checker: bool = True
    onnx_shape_inference: bool = True
    onnx_strict_mode: bool = False
    onnx_check_type: bool = False
    onnx_data_prop: bool = True

    input_names: Optional[List[str]] = None
    output_names: Optional[List[str]] = None
    do_constant_folding: bool = True
    operator_export_type: OperatorExportTypes = OperatorExportTypes.ONNX

    training: Optional[torch.onnx.TrainingMode] = None

    dynamic_axes: Any = None
    custom_opsets: Dict = dataclasses.field(default_factory=dict)


class _Exporter(_ExporterOptions):
    def __init__(self, model: Callable, inputs: tuple, **opts: Any):
        super().__init__(**opts)

        # Load symbolic opset
        assert self.opset_version is not None
        sym_reg.register_version("", self.opset_version)  # type: ignore[no-untyped-call]

        self.original_model = model
        self.inputs = inputs

        self.attrs: Dict[TorchValueID, ONNXValueID] = {}
        self.node_doc_string: Dict[torch._C.Node, str] = {}
        self.node_scope: Dict[torch._C.Node, str] = {}

        self._convert()

    def _run_trace(self) -> None:
        # TODO(twata): Use `torch._C._craete_graph_by_tracing` instead.
        # So that we don't need to run heavy models multiple times
        self.traced: torch.jit.RecursiveScriptModule = torch.jit.trace(  # type: ignore
            self.original_model,
            self.inputs,
            check_trace=self.check_trace,
            strict=self.strict_trace,
            _force_outplace=self.force_outplace_trace,
        )

        self.graph_doc_string = f"""
# Model: {self.traced.original_name}
"""

        self.outputs = self.traced(*self.inputs)
        if not isinstance(self.outputs, tuple):
            self.outputs = (self.outputs,)
        self.g: torch._C.Graph = self.traced.inlined_graph
        self.vars = self.traced.state_dict()
        self.self_id: Optional[TorchValueID] = None
        first_arg = list(self.g.inputs())[0]
        if first_arg.type().kind() == "ClassType":
            self.self_id = _unique_id(first_arg)
        self.log("Inlined graph", self.g)

        to_utils._params_dict = self.vars  # type: ignore[attr-defined]

        # torch.jit level preprocess
        # TODO(twata): Pass tot
        self.g = self.optimize(self.g)
        self.log("Optimized graph", self.g)

        self.log("Original traced graph", self.traced.graph)
        self.log("State dict", "\n".join([f"- {k}: {v}" for k, v in self.vars.items()]))

    def is_self(self, v: torch._C.Value) -> bool:
        return _unique_id(v) == self.self_id

    # Run jit pass with post lint
    def run_jit_pass(self, p: Callable, g: torch._C.Graph, *args: object) -> None:
        p(g)
        torch._C._jit_pass_lint(g, *args)

    # torch level graph optimizer based on `to_utils._optimize_graph`
    def optimize(self, graph: torch._C.Graph) -> torch._C.Graph:
        self.run_jit_pass(torch._C._jit_pass_inline_fork_wait, graph)  # type: ignore[attr-defined]
        if self.torch_constant_prop:
            self.run_jit_pass(torch._C._jit_pass_constant_propagation, graph)  # type: ignore[attr-defined]

        # _split_tensor_list_constants(graph, graph)
        # run dce to eliminate dead parts of the graph that might have been
        # left behind by things like symbolic_override
        self.run_jit_pass(torch._C._jit_pass_dce, graph)

        self.run_jit_pass(torch._C._jit_pass_canonicalize_graph_fuser_ops, graph)  # type: ignore[attr-defined]
        torch._C._jit_pass_peephole(graph, True)  # type: ignore[attr-defined]
        self.run_jit_pass(torch._C._jit_pass_fuse_addmm, graph)  # type: ignore[attr-defined]

        torch._C._jit_pass_peephole(graph, True)  # type: ignore[attr-defined]
        torch._C._jit_pass_lower_all_tuples(graph)  # type: ignore[attr-defined]
        # in _jit_pass_onnx, symbolic functions are called for each node for conversion.
        # However, there are nodes that cannot be converted without additional context.
        # For example, the number of outputs from split
        # (and whether it is static or dynamic) is unknown
        # until the point where it is unpacked by listUnpack node.
        # This pass does a preprocess, and prepares the nodes such that enough
        # context can be received
        # by the symbolic function.
        # torch._C._jit_pass_onnx_remove_inplace_ops_for_onnx(graph, None)
        torch._C._jit_pass_onnx_preprocess(graph)  # type: ignore[attr-defined]

        # onnx does not support tuples, so try to remove them
        torch._C._jit_pass_lint(graph)

        # onnx only supports tensors, but 1 / 2 = 0.5 and tensor(1) / tensor(2) = 0
        torch._C._jit_pass_prepare_division_for_onnx(graph)  # type: ignore[attr-defined]

        torch._C._jit_pass_onnx_remove_print(graph)  # type: ignore[attr-defined]
        torch._C._jit_pass_onnx_preprocess_caffe2(graph)  # type: ignore[attr-defined]

        if self.operator_export_type == torch.onnx.OperatorExportTypes.ONNX_ATEN_FALLBACK:
            sym_hel._quantized_ops.clear()
            # Unpack quantized weights for conv and linear ops and insert into graph.
            torch._C._jit_pass_onnx_unpack_quantized_weights(graph, self.vars)  # type: ignore[attr-defined]
            # Insert permutes before and after each conv op to ensure correct order.
            torch._C._jit_pass_onnx_quantization_insert_permutes(graph, self.vars)  # type: ignore[attr-defined]

            # Find consecutive permutes that are no-ops and remove them.
            torch._C._jit_pass_custom_pattern_based_rewrite_graph(  # type: ignore[attr-defined]
                """
            graph(%Pi):
                %Pq = quantized::nhwc2nchw(%Pi)
                %Pr = quantized::nchw2nhwc(%Pq)
                return (%Pr)""",
                """
            graph(%Ri):
                return (%Ri)""",
                graph,
            )

        # onnx only supports tensors, so we turn all out number types into tensors
        torch._C._jit_pass_erase_number_types(graph)  # type: ignore[attr-defined]

        input_names = self.input_names.copy()
        if input_names is not None:
            assert len(list(graph.inputs())) == len(input_names) + (0 if self.self_id is None else 1)
            inputs = list(graph.inputs())
            self_count = 0
            for idx, n in enumerate(input_names):
                if _unique_id(inputs[idx + self_count]) == self.self_id:
                    self_count += 1
                    continue
                inputs[idx + self_count].setDebugName(n)
            if self_count > 0:
                input_names.insert(0, inputs[0].debugName())
        torch._C._jit_pass_onnx_set_dynamic_input_shape(  # type: ignore[attr-defined]
            graph, self.dynamic_axes or {}, input_names or []
        )

        return graph

    def log(self, title: str, v: Any, debug: bool = False) -> None:
        if not (self.verbose or debug):
            return

        s = f"""
## {title}
{v}
"""
        print(s)

        if self.strip_doc_string:
            return

        self.graph_doc_string += s

    def handle_constant(self, g: torch._C.Graph, n: torch._C.Node) -> None:
        # Skip None constant node
        if not n.hasAttribute("value"):
            return

        def gen_const(g: torch._C.Graph, value: Any = None) -> torch._C.Value:
            c = g.op("Constant")
            if n.kindOf("value") == "ival":
                ival = n.output().toIValue()
                if isinstance(ival, list) and not isinstance(ival[0], (int, float)):
                    vals = []
                    for i in ival:
                        if isinstance(i, torch.Tensor):
                            vals.append(g.op("Constant", value_t=i))
                        else:
                            vals.append(g.op("Constant"))
                    c = g.op("prim::ListConstruct")
                    for v in vals:
                        c.node().addInput(v)
                else:
                    c.node().t_("value", torch.tensor(ival))
            else:
                c.node().copyAttributes(n)
            return c

        self.run_symbolic_function(g, n, gen_const)

    def handle_getattr(self, g: torch._C.Graph, n: torch._C.Node) -> None:
        if self.is_self(n.input()):
            self.attrs[_unique_id(n.output())] = ONNXValueID(n.s("name"))
        else:
            self.attrs[_unique_id(n.output())] = ONNXValueID(
                "%s.%s"
                % (
                    self.attrs[_unique_id(n.input())],
                    n.s("name"),
                )
            )
        var_name = self.attrs[_unique_id(n.output())]
        if var_name in self.vars:
            n.output().inferTypeFrom(self.vars[var_name])

    def handle_list_construct(self, g: torch._C.Graph, n: torch._C.Node) -> None:
        # Concat if int type input
        is_integer_output: bool = n.output().type().getElementType().kind() == "IntType"
        if len(list(n.inputs())) > 0 and is_integer_output:

            def gen_concat(g: torch._C.Graph, *args: Any) -> torch._C.Value:
                seq: List[torch._C.Value] = []
                for i in args:
                    if i.type().kind() == "IntType" or len(i.type().sizes()) == 0:
                        seq.append(
                            sym_hel._unsqueeze_helper(g, i, axes_i=[0])  # type: ignore[no-untyped-call,call-arg]
                        )
                    else:
                        seq.append(i)
                return g.op("Concat", *seq, axis_i=0)

            self.run_symbolic_function(g, n, gen_concat)
        else:

            def gen_seq(g: torch._C.Graph, *args: Any) -> torch._C.Value:
                if len(args) == 0:
                    return g.op("SequenceEmpty")  # TODO(twata): Set dtype attribute
                else:
                    return g.op("SequenceConstruct", *args)

            self.run_symbolic_function(g, n, gen_seq)

    def handle_if(self, g: torch._C.Graph, n: torch._C.Node) -> None:
        # Generated onnx node doc string should be added later since DCE isn't completed yet
        doc_str: str = f"""
## Original node
{n}
## Scope
{n.scopeName()}
## Source Range
```
{n.sourceRange()}
```
"""

        # If node will reused to keep graph lint happy
        for b in n.blocks():
            block_nodes = list(b.nodes())
            for b_n in block_nodes:
                self.generate_onnx_node(cast(torch._C.Graph, b), b_n)

        if not self.strip_doc_string:
            self.node_doc_string[n] = doc_str

        # Move to last of graph to keep the execution order of node
        n.moveBefore(g.return_node())

    handler: Dict[str, Callable] = {
        "prim::Constant": handle_constant,
        "prim::GetAttr": handle_getattr,
        "prim::ListConstruct": handle_list_construct,
        "prim::If": handle_if,
    }

    def symbolic_function(self, n: torch._C.Node) -> Optional[Callable]:
        ns, op = n.kind().split("::")
        if op.endswith("_"):  # For inplace op
            op = op[:-1]
        if ns == "prim" and op == "PythonOp":
            pyobj = n.pyobj()
            if issubclass(pyobj.__self__, torch.autograd.Function):
                pyobj = pyobj.__self__
            assert issubclass(pyobj, torch.autograd.Function)
            assert hasattr(pyobj, "symbolic"), f"symbolic method not supported in {pyobj}"
            # TODO(twata): Use repr(pyobj) in scope name or doc_string
            return cast(Callable, pyobj.symbolic)
        else:
            if ns == "prim":
                op = f"prim_{op}"
            if sym_reg.is_registered_op(op, "", self.opset_version):  # type: ignore[no-untyped-call]
                return cast(
                    Callable, sym_reg.get_registered_op(op, "", self.opset_version)  # type: ignore[no-untyped-call]
                )
            else:
                return None

    def run_symbolic_function(self, g: torch._C.Graph, n: torch._C.Node, sym_func: Callable) -> None:
        attrs: Dict[str, Any] = {}
        for a in n.attributeNames():
            if a == "value" and n.kindOf("value") == "ival":
                attrs[a] = n.output().toIValue()
            else:
                attrs[a] = n[a]
        if "inplace" in attrs:
            del attrs["inplace"]
        node_inputs = list(n.inputs())
        if n.kind() ==  "prim::PythonOp":
            node_inputs.extend(n.scalar_args())
        sym_outs = sym_func(g, *node_inputs, **attrs)
        if not isinstance(sym_outs, (list, tuple)):
            sym_outs = [sym_outs]
        assert len(sym_outs) == n.outputsSize(), f"{sym_outs}: {len(sym_outs)} vs {n.outputsSize()}"

        def list_added_nodes() -> List[torch._C.Node]:
            start_vals: Set[torch._C.Value] = set(list(n.inputs()))
            ret: Set[torch._C.Node] = set()
            target_vals: List[torch._C.Value] = list(sym_outs)
            for i in sym_outs:
                if i in start_vals:
                    continue
                ret.add(i.node())
                target_vals.extend(list(i.node().inputs()))
            while len(target_vals) > 0:
                i = target_vals.pop()
                if i in start_vals:
                    continue
                ret.add(i.node())
                target_vals.extend(list(i.node().inputs()))
            return list(ret)

        sym_nodes: List[torch._C.Node] = list_added_nodes()

        self.log(f"Converting node {n.kind()}", n)
        if len(sym_nodes) > 0:
            self.log(f"Converted node {n.kind()}", "\n".join([str(i) for i in sym_nodes]))

        # Generate doc string before old node lifetime ends
        for sym_nd in sym_nodes:
            if not self.strip_doc_string:
                self.node_doc_string[sym_nd] = onnx_node_doc_string(sym_nd, n)
            self.node_scope[sym_nd] = n.scopeName()

        # Replace uses of old node output with symbolic outputs
        for old_out, new_out in zip(n.outputs(), sym_outs):
            old_out.replaceAllUsesWith(new_out)
            assert len(old_out.uses()) == 0
            new_out.copyMetadata(old_out)

    def generate_onnx_node(self, g: torch._C.Graph, n: torch._C.Node) -> None:
        node_kind: str = n.kind()
        if node_kind in self.handler:
            self.handler[node_kind](self, g, n)
            return

        f: Optional[Callable] = self.symbolic_function(n)
        if self.operator_export_type in [OperatorExportTypes.ONNX_ATEN, OperatorExportTypes.ONNX_FALLTHROUGH] or (
            self.operator_export_type == OperatorExportTypes.ONNX_ATEN_FALLBACK and f is None
        ):
            def gen_aten_node(g: torch._C.Graph, *inputs):
                ret = g.op("ATen", *inputs, outputs=len(list(n.outputs())))
                v = ret if n.outputsSize() == 1 else ret[-1]
                v.node().copyAttributes(n)
                v.node().s_("operator", n.kind().split("::")[-1])
                return ret

            f = gen_aten_node
        assert f is not None, f"Symbolic function for {n.kind()} not found"
        self.run_symbolic_function(g, n, f)

    def check_model(self, model: onnx.ModelProto) -> onnx.ModelProto:
        if self.onnx_shape_inference:
            model = onnx.shape_inference.infer_shapes(
                model,
                check_type=self.onnx_check_type,
                strict_mode=self.onnx_strict_mode,
                data_prop=self.onnx_data_prop,
            )
        if self.enable_onnx_checker:
            onnx.checker.check_model(model)

        return model

    def generate_proto_nodes(
        self,
        g: torch._C.Graph,
        onnx_vars: Dict[TorchValueID, onnx.TensorProto],
        val_tab: Dict[TorchValueID, ONNXValueID],
    ) -> Tuple[List[onnx.NodeProto], Dict[TorchValueID, onnx.TensorProto], Dict[TorchValueID, ONNXValueID],]:
        node_name_counter: int = 0

        def node_name(n: torch._C.Node) -> str:
            nonlocal node_name_counter
            op = n.kind().split("::")[-1]
            node_name_counter += 1
            return f"{op}_{node_name_counter}"

        def value_name(v: torch._C.Value) -> ONNXValueID:
            if _unique_id(v) in self.attrs:
                return self.attrs[_unique_id(v)]

            n: torch._C.Node = v.node() or v.uses()[0].user
            scope: str = self.node_scope.get(n, n.scopeName())
            scope = _remove_prefix(scope.split("/")[-1], "__module.")
            if len(scope) > 0:
                scope += "."
            return ONNXValueID(f"{scope}{v.debugName()}")

        def block2subgraph(name: str, b: torch._C.Block, doc_string: str) -> onnx.GraphProto:
            branch_nodes, _, _ = self.generate_proto_nodes(cast(torch._C.Graph, b), onnx_vars, val_tab)
            branch_inputs: List[onnx.ValueInfoProto] = []
            for i in b.inputs():
                branch_inputs.append(onnx.ValueInfoProto())
                branch_inputs[-1].name = val_tab[_unique_id(i)]
                if not self.strip_doc_string:
                    branch_inputs[-1].doc_string = repr(i)
            branch_outputs: List[onnx.ValueInfoProto] = []
            for i in b.outputs():
                branch_outputs.append(onnx.ValueInfoProto())
                branch_outputs[-1].name = val_tab[_unique_id(i)]
                if not self.strip_doc_string:
                    branch_outputs[-1].doc_string = repr(i)

            branch_graph: onnx.GraphProto = onnx.helper.make_graph(
                name=name,
                nodes=branch_nodes,
                # TODO(twata): Support initializers if needed
                inputs=branch_inputs,
                outputs=branch_outputs,
                doc_string=doc_string,
            )

            return branch_graph

        # Nodes and initializers
        onnx_nodes: List[onnx.NodeProto] = []
        self_count: int = 0
        # Run only in root graph
        if self.g == g:
            if self.input_names is not None:
                for idx, v in enumerate(g.inputs()):
                    if self.is_self(v):  # Skip module's self input
                        self_count += 1
                        continue
                    val_tab[_unique_id(v)] = ONNXValueID(self.input_names[idx - self_count])
                assert (len(list(g.inputs())) - self_count) == len(self.input_names)
            if self.output_names is not None:
                if len(self.output_names) != len(list(g.outputs())):
                    warnings.warn(f"Specified output_names ({self.output_names}) count and graph outputs ({list(g.outputs())}) count differ")
                for idx, v in enumerate(g.outputs()):
                    if idx >= len(self.output_names):
                        break
                    val_tab[_unique_id(v)] = ONNXValueID(self.output_names[idx])
        for n in g.nodes():
            if n.kind() == "prim::GetAttr":
                continue
            if n.kind() == "onnx::Constant" and len(n.output().uses()) == 0:
                continue
            for i in n.inputs():
                if self.is_self(i):
                    continue
                if _unique_id(i) in self.attrs:
                    if _unique_id(i) not in onnx_vars:
                        k: ONNXValueID = self.attrs[_unique_id(i)]
                        t: torch.Tensor = self.vars[k]
                        onnx_vars[_unique_id(i)] = _tensor_to_proto(t, name=k)
                k = value_name(i)
                if _unique_id(i) not in val_tab:
                    val_tab[_unique_id(i)] = k

            for o in n.outputs():
                k = value_name(o)
                if _unique_id(o) not in val_tab:
                    val_tab[_unique_id(o)] = k

            def assign_onnx_values(
                onnx_values: List[str],
                prefix: str,
                torch_values: Iterator[torch._C.Value],
            ) -> None:
                for v in torch_values:
                    k: ONNXValueID = val_tab.get(_unique_id(v), value_name(v))
                    if _unique_id(v) in val_tab:
                        assert val_tab[_unique_id(v)] == k
                    val_tab[_unique_id(v)] = k
                    onnx_values.append(k)

            new_nd = onnx.NodeProto()
            new_nd.name = node_name(n)
            new_nd.op_type = n.kind().split("::")[-1]
            if n.kind() == "prim::If":
                if n in self.node_doc_string:
                    new_nd.doc_string = f"""## Symbolic node
{n}
{self.node_doc_string[n]}"""
                blocks: List[torch._C.Block] = list(n.blocks())
                assert len(blocks) == 2
                for attr_name, block in zip(["then_branch", "else_branch"], blocks):
                    sub_g = block2subgraph(f"{new_nd.name}_{attr_name}", block, new_nd.doc_string)
                    new_nd.attribute.append(onnx.helper.make_attribute(attr_name, sub_g))
            else:
                assert len(list(n.blocks())) == 0, f"Node with block needs to be handled separately: {n}"
                if n in self.node_doc_string:
                    new_nd.doc_string = self.node_doc_string[n]
                for attr_name in n.attributeNames():
                    if n.kindOf(attr_name) == "t":
                        attr = onnx.helper.make_attribute(attr_name, _tensor_to_proto(n.t(attr_name)))
                    else:
                        attr = onnx.helper.make_attribute(attr_name, n[attr_name])
                    new_nd.attribute.append(attr)
            assign_onnx_values(new_nd.input, new_nd.name, n.inputs())
            assign_onnx_values(new_nd.output, new_nd.name, n.outputs())
            onnx_nodes.append(new_nd)

        return onnx_nodes, onnx_vars, val_tab

    def generate_onnx(self) -> onnx.ModelProto:
        # Convert prim and aten nodes to ONNX by using symbolic functions
        self.original_g: torch._C.Graph = self.g.copy()
        target_nodes = list(self.g.nodes())
        for n in target_nodes:
            self.generate_onnx_node(self.g, n)

        # Remove old prim and aten nodes by running DCE
        # After nodes is emited to ONNX nodes, all side effects should be removed
        self.run_jit_pass(
            torch._C._jit_pass_dce_allow_deleting_nodes_with_side_effects, self.g  # type: ignore[attr-defined]
        )

        if self.do_constant_folding and self.opset_version in torch.onnx.constant_folding_opset_versions:
            folded: Dict[str, torch.IValue] = torch._C._jit_pass_onnx_constant_fold(  # type: ignore[attr-defined]
                self.g, self.vars, self.opset_version
            )
            # Replace input with constant nodes
            input_table: Dict[str, torch._C.Value] = {i.debugName(): i for i in self.g.inputs()}
            for k, t in folded.items():
                c: torch._C.Value = self.g.create("onnx::Constant", 1).output()
                assert isinstance(t, torch.Tensor)
                c.node().t_("value", cast(torch.Tensor, t))
                self.g.prependNode(c.node())
                # TODO(twata): Determine foleded nodes from original graph and document it
                self.node_doc_string[c.node()] = f"Constant folded node: {input_table[k]}"
                input_table[k].replaceAllUsesWith(c)
                c.copyMetadata(input_table[k])
                del input_table[k]
            for _ in range(len(list(self.g.inputs())) - len(input_table)):
                self.g.eraseInput(len(input_table))
            torch._C._jit_pass_dce_allow_deleting_nodes_with_side_effects(self.g)  # type: ignore[attr-defined]

        self.log("ONNX graph", self.g)

        onnx_nodes, onnx_vars, val_tab = self.generate_proto_nodes(self.g, {}, {})

        def onnx_value(v: torch._C.Value, name: ONNXValueID) -> onnx.ValueInfoProto:
            return onnx.helper.make_value_info(
                name,
                None if v.type() is None else _type_to_proto(cast(torch._C.TensorType, v.type())),
                doc_string=None if self.strip_doc_string else repr(v),
            )

        # Values
        onnx_inputs: List[onnx.ValueInfoProto] = []
        inout_names: List[str] = []
        self_count = 0
        for idx, v in enumerate(self.g.inputs()):
            if self.is_self(v):  # Skip module's self input
                self_count += 1
                continue
            assert len(v.uses()) > 0
            k = val_tab[_unique_id(v)]
            inout_names.append(k)
            onnx_inputs.append(onnx_value(v, k))
            _apply_tensor_info_to_value_info(onnx_inputs[-1], self.inputs[idx - self_count])
        onnx_outputs: List[onnx.ValueInfoProto] = []
        for idx, v in enumerate(self.g.outputs()):
            k = val_tab[_unique_id(v)]
            inout_names.append(k)
            onnx_outputs.append(onnx_value(v, k))
            _apply_tensor_info_to_value_info(onnx_outputs[-1], self.outputs[idx])

        graph = onnx.helper.make_graph(
            nodes=onnx_nodes,
            name=self.traced.original_name,
            inputs=onnx_inputs,
            outputs=onnx_outputs,
            initializer=[v for k, v in onnx_vars.items()],
            doc_string=None if self.strip_doc_string else self.graph_doc_string,
            # TODO(twata): Use torch IR's value type info
            # value_info=[
            #     self.values[k] for k in set(list(self.values.keys())) - set(inout_names)
            # ],
        )

        self.log("ONNX printable graph", onnx.helper.printable_graph(graph))

        model = onnx.helper.make_model(
            graph,
            opset_imports=[onnx.helper.make_opsetid("", self.opset_version)],
        )
        return self.check_model(model)

    def _convert(self) -> None:
        prev_opset_version = None
        prev_export_type = None
        prev_shape_inference = None
        try:
            assert not to_utils.is_in_onnx_export()  # type: ignore[no-untyped-call]
            with to_utils.select_model_mode_for_export(self.original_model, self.training):
                to_utils.__IN_ONNX_EXPORT = True
                prev_opset_version = sym_hel._export_onnx_opset_version
                sym_hel._set_opset_version(self.opset_version)  # type: ignore[no-untyped-call]
                prev_export_type = sym_hel._operator_export_type
                sym_hel._set_operator_export_type(self.operator_export_type)  # type: ignore[no-untyped-call]
                prev_shape_inference = sym_hel._onnx_shape_inference
                sym_hel._set_onnx_shape_inference(  # type: ignore[no-untyped-call]
                    False  # TODO(twata): Use `self.onnx_shape_inference`
                )
                self._run_trace()
                self.model: onnx.ModelProto = self.generate_onnx()
        finally:
            to_utils.__IN_ONNX_EXPORT = False
            if prev_opset_version is not None:
                sym_hel._set_opset_version(prev_opset_version)  # type: ignore[no-untyped-call]
            if prev_shape_inference is not None:
                sym_hel._set_operator_export_type(prev_export_type)  # type: ignore[no-untyped-call]
            if prev_shape_inference is not None:
                sym_hel._set_onnx_shape_inference(prev_shape_inference)  # type: ignore[no-untyped-call]

    def generate(self, f: Union[str, typing.IO]) -> None:
        if isinstance(f, str):
            with open(f, "wb") as o:
                o.write(self.model.SerializeToString())
        else:
            f.write(self.model.SerializeToString())


def export(
    model: Callable,
    args: tuple,
    f: Union[str, typing.IO],
    **kwargs: object,
) -> Any:
    ex = _Exporter(model, args, **kwargs)
    ex.generate(f)

    return ex.outputs

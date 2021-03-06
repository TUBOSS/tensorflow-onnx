# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""
tf2onnx.rewriter.lstm_rewriter - lstm support
"""

from __future__ import division
from __future__ import print_function

from tf2onnx.rewriter.unit_rewriter_base import *

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tf2onnx.rewriter.lstm_rewriter")


class LSTMUnitRewriter(UnitRewriterBase):
    def __init__(self, g):
        super(LSTMUnitRewriter, self).__init__(g)
        self.switch_checkers = {
            # True means we need parse its initial value in later logic.
            "ct": (self._ct_switch_check, self._connect_lstm_yc_to_graph, True),
            "ht": (self._ht_switch_check, self._connect_lstm_yh_to_graph, True),
            "ct_ht": (self._ct_ht_shared_switch_check, self._connect_lstm_ych_to_graph, True),
            "output": (self._output_switch_check, self._connect_lstm_output_to_graph, False),
        }

    def run(self):
        return super(LSTMUnitRewriter, self).run(RNNUnitType.LSTMCell)

    def get_rnn_scope_name(self, match):
        # take the cell output and go up 3 levels to find the scope:
        # name of h is like root/while/lstm_cell/mul_2
        # root is the dynamic rnn's scope name. 
        # root/while/lstm_cell is cell's scope name
        h_node = match.get_op("ht")
        parts = h_node.name.split('/')
        rnn_scope_name = '/'.join(parts[0:-3])
        return rnn_scope_name

    def get_weight_and_bias(self, match):
        # if one of them is not match, just return
        w_e = match.get_op("cell_kernel")
        w = get_weights_from_const_node(w_e)
        if not w:
            return

        # check https://www.tensorflow.org/versions/r1.8/api_docs/cc/class/tensorflow/ops/bias-add
        # for bias_add data format
        bias_add = match.get_op("bias_add")
        if bias_add.data_format != "NHWC":
            log.debug("BiasAdd data_format is not NHWC, SKIP")
            return

        b_e = match.get_op("cell_bias")
        b = get_weights_from_const_node(b_e)
        if not b or b.value.shape[0] != w.value.shape[1]:
            log.warning("cell_kernel and cell_bias's dimensions does not match, skip")
            return 

        ft_bias = match.get_op("ft_bias")
        ft = get_weights_from_const_node(ft_bias)
        if not ft:
            return

        if not (len(ft.value) == 1 and b_e.dtype == ft_bias.dtype):
            return

        return RnnWeights(w, b, ft)

    def _ct_switch_check(self, enter_target_node_input_id, identity_consumers, match):
        # original we use c.inputs[0] == match.get_op("ft") to check c initializer for LSTMCell
        # but in BasicLSTMCell, c.inputs[1] is "ft", that's because BasicLSTMCell and LSTMCell's call 
        # function are defining the multiplication with different order. So we change to match.get_op("ft") in c.inputs
        mul_nodes = [c for c in identity_consumers if c.type == "Mul" and match.get_op("ft") in c.inputs]
        if len(mul_nodes) == 1:
            log.debug("find c initializer value at " + enter_target_node_input_id)
            return enter_target_node_input_id
        else:
            log.debug("multiple Mul matching found, cannot identify c initializer")
            return None

    def _ht_switch_check(self, enter_target_node_input_id, identity_consumers, match):
        concat_nodes = [c for c in identity_consumers if c == match.get_op("xh")]
        if len(concat_nodes) == 1:
            log.debug("find h initializer value at " + enter_target_node_input_id)
            return enter_target_node_input_id
        else:
            log.debug(str(len(concat_nodes)) + "Concat matching found, cannot identify h initializer")
            return None

    # when state is not tuple, ct and ht may share same switch.
    def _ct_ht_shared_switch_check(self, enter_target_node_input_id, identity_consumers, match):
        slices = [c for c in identity_consumers if c.type == "Slice"]
        if not slices:
            log.debug("find no switch_identity_slice nodes")
            return None

        c_slice = None
        h_slice = None
        hidden_size = None
        for s in slices:
            slice_consumers = self.g.find_output_consumers(s.output[0])
            if len(slice_consumers) != 1:
                continue

            s_begin = s.inputs[1].get_tensor_value()
            s_size = s.inputs[2].get_tensor_value()
            hidden_size = s_size[1]
            if list(s_begin) == [0, 0]:
                c_slice = s
            elif list(s_begin) == [0, hidden_size]:
                h_slice = s

        if c_slice and h_slice:
            log.debug("find c_h shared initializer value at " + enter_target_node_input_id) 
            return enter_target_node_input_id
        return None

    def _output_switch_check(self, enter_target_node_input_id, identity_consumers, match):
        ta_write_nodes = [c for c in identity_consumers if c.type == "TensorArrayWriteV3"]
        if len(ta_write_nodes) == 1:
            enter_target_node = self.g.get_node_by_name(enter_target_node_input_id)
            if enter_target_node.type == "TensorArrayV3":
                log.debug("found output switch node")
                return enter_target_node_input_id
            log.debug("found enter target node is not ta node")
            return None
        log.debug(str(len(ta_write_nodes)) + " TensorArrayWriteV3 matching found, cannot validate output switch")
        return None

    def process_input_x(self, rnn_props, rnn_scope_name):
        self.print_step("look for possible transpose following RNN input node")
        # todo: peepholdes P is not considered now
        input_consumers = self.g.find_output_consumers(rnn_props.input_id)
        consumers_in_rnn_scope = []
        for consumer in input_consumers:
            if consumer.name.startswith(rnn_scope_name):
                consumers_in_rnn_scope.append(consumer)

        if len(consumers_in_rnn_scope) != 1:
            log.error("RNN input node has " + str(len(consumers_in_rnn_scope)) +
                      " consumers in current rnn scope " + rnn_scope_name + ", skip")
            return None

        possible_transpose_after_input = consumers_in_rnn_scope[0]

        self.print_step("convert the transpose to onnx node if there is one found.")
        # check whether time_major is enabled or not
        # in TF, if time_major is not enabled, input format is [batch, time, ...]
        # but, during TF handling, at the beginning, the data will be transposed to [time, batch, ...]
        # after processing, the format is changed back before returning result.
        # So here, we judge the time_major by checking the transpose operator existence.
        converted_transpose = self._convert_timemajor_transpose(possible_transpose_after_input)
        if converted_transpose:
            log.debug("detect batch-major inputs")
            rnn_props.time_major = False
            rnn_props.x_input_id = converted_transpose.output[0]
            self.all_nodes.extend([converted_transpose])
        else:
            log.debug("detect timer-major inputs")
            rnn_props.time_major = True
            rnn_props.x_input_id = rnn_props.input_id

        rnn_props.onnx_input_ids["X"] = rnn_props.x_input_id
        return rnn_props

    def _convert_timemajor_transpose(self, node):
        if not check_is_timemajor_transpose(node):
            log.debug("not found timemajor transpose")
            return

        log.debug("found timemajor transpose")

        attr = {"perm": np.array([1, 0, 2], dtype=np.int64)}
        new_trans = make_onnx_node(self.g, "Transpose", [node.input[0]], attr)

        self.g.copy_shape(node.output[0], new_trans.output[0])
        self.g.replace_all_inputs(self.g.get_nodes(), node.output[0], new_trans.output[0])
        return new_trans

    def process_weights_and_bias(self, rnn_weights, rnn_props):
        w_r_icfo = rnn_weights.kernel.value
        w_dtype = rnn_weights.kernel.dtype
        b_r_icfo = rnn_weights.bias.value
        b_dtype = rnn_weights.bias.dtype
        ft_bias_scalar = rnn_weights.forget_bias.value

        # split bias for each hidden unit
        # b_r_icfo: (4 * num_units,)
        bias_dim = b_r_icfo.shape[0]
        hidden_size = int(bias_dim/4)
        b_r_icfo = np.reshape(b_r_icfo, (1, bias_dim))
        bias_gates = np.split(b_r_icfo, 4, axis=1)
        ft_bias = np.add(bias_gates[2], ft_bias_scalar[0])
        wb_bias_iofc = np.concatenate((bias_gates[0], bias_gates[3], ft_bias, bias_gates[1]), axis=1)

        # fill Rb with empty since in TF, we have only one bias.
        rb_bias_iofc = np.zeros((1, bias_dim), dtype=b_dtype)
        B = np.concatenate((wb_bias_iofc, rb_bias_iofc), axis=1)
        assert B.shape == (1, 2 * bias_dim)

        [wx, wh] = np.split(w_r_icfo, [-1 * hidden_size])
        input_size = wx.shape[0]
        assert wx.shape[0] == input_size
        assert int(wx.shape[1]/4) == hidden_size

        # split weight for gates
        w_gates = np.split(wx, 4, axis=1)
        new_wx = np.concatenate((w_gates[0], w_gates[3], w_gates[2], w_gates[1]), axis=1)

        h_gates = np.split(wh, 4, axis=1)
        new_wh = np.concatenate((h_gates[0], h_gates[3], h_gates[2], h_gates[1]), axis=1)
        W_iofc = np.transpose(new_wx)
        R_iofc = np.transpose(new_wh)

        W = np.array([W_iofc], w_dtype)
        R = np.array([R_iofc], w_dtype)

        # create node
        w_name = utils.make_name("W")
        w_node = self.g.make_const(w_name, W, skip_conversion=True)

        r_name = utils.make_name("R")
        r_node = self.g.make_const(r_name, R, skip_conversion=True)

        b_name = utils.make_name("B")
        b_node = self.g.make_const(b_name, B, skip_conversion=True)

        rnn_props.input_size = input_size
        rnn_props.hidden_size = hidden_size
        rnn_props.onnx_input_ids["W"] = w_node.output[0]
        rnn_props.onnx_input_ids["R"] = r_node.output[0]
        rnn_props.onnx_input_ids["B"] = b_node.output[0]
        return input_size, hidden_size

    def process_var_init_nodes(self, rnn_props):
        init_h_id = None
        init_c_id = None
        if "ct_ht" in rnn_props.var_initializers:
            init_h_id, init_c_id = self._process_non_tuple_ch_init_nodes(rnn_props)
        elif "ct" in rnn_props.var_initializers and "ht" in rnn_props.var_initializers:
            init_h_id, init_c_id = self._process_tuple_ch_init_nodes(rnn_props)
        else:
            raise ValueError("no initializers, unexpected")
        assert init_h_id and init_c_id
        rnn_props.onnx_input_ids["initial_h"] = init_h_id
        rnn_props.onnx_input_ids["initial_c"] = init_c_id

    # todo: refine when implementing GRU
    def _process_non_tuple_ch_init_nodes(self, rnn_props):
        input_id = rnn_props.var_initializers["ct_ht"]
        hidden_size = rnn_props.hidden_size

        # todo: remove this once Fill ops is supported 
        fill_ch_init_node = self._workaround_fill_ch_init_node(input_id, rnn_props)
        if fill_ch_init_node: 
            return fill_ch_init_node.output[0], fill_ch_init_node.output[0]

        attr = {"axes": [1], "starts": [0], "ends": [hidden_size]}
        slice_node1 = make_onnx_node(self.g, "Slice", [input_id], attr)
        unsqueeze_node_1 = make_onnx_node(self.g, "Unsqueeze", [slice_node1.output[0]], attr={"axes": [0]})

        attr = {"axes": [1], "starts": [hidden_size], "ends": [hidden_size*2]}
        slice_node2 = make_onnx_node(self.g, "Slice", [input_id], attr)
        unsqueeze_node_2 = make_onnx_node(self.g, "Unsqueeze", [slice_node2.output[0]], attr={"axes": [0]})

        self.all_nodes.extend([slice_node1, slice_node2, unsqueeze_node_1, unsqueeze_node_2])
        self.must_keep_nodes.append(self.g.get_node_by_name(input_id))
        return unsqueeze_node_1.output[0], unsqueeze_node_2.output[0]

    def _process_tuple_ch_init_nodes(self, rnn_props):
        h_init_input_id = rnn_props.var_initializers["ht"]
        c_init_input_id = rnn_props.var_initializers["ct"]
        h_node_output = self._process_c_or_h_init_nodes(h_init_input_id, rnn_props)
        c_node_output = self._process_c_or_h_init_nodes(c_init_input_id, rnn_props)
        return h_node_output, c_node_output

    def _process_c_or_h_init_nodes(self, initializer_input_id, rnn_props):
        # todo: remove this once Fill ops is supported
        fill_ch_init_node = self._workaround_fill_ch_init_node(initializer_input_id, rnn_props) 
        if fill_ch_init_node: 
            return fill_ch_init_node.output[0]

        node = self.g.get_node_by_name(initializer_input_id)
        self.must_keep_nodes.append(node)
        if node.is_const():
            val = node.get_tensor_value()
            initial_name = utils.make_name("Const")
            new_val = np.expand_dims(val, axis=0)
            const_node = self.g.make_const(initial_name, new_val)
            return const_node.output[0]
        else:
            squeeze_node = make_onnx_node(self.g, "Unsqueeze", [initializer_input_id], attr={"axes": [0]})
            self.g.replace_all_inputs(self.g.get_nodes(), initializer_input_id, squeeze_node.output[0])
            self.all_nodes.append(squeeze_node)
            return squeeze_node.output[0]

    def _workaround_fill_ch_init_node(self, initializer_input_id, rnn_props):
        node = self.g.get_node_by_name(initializer_input_id)
        if node.type != "Fill":
            return 

        self.must_keep_nodes.remove(node)

        fill_val = node.inputs[1].get_tensor_value()[0]
        fill_val_dtype = utils.ONNX_TO_NUMPY_DTYPE[node.inputs[1].dtype]

        # this must be int64, since Concat's input data type must be consistent.
        num_direction_node = self.g.make_const(utils.make_name("Const"), np.array([1], dtype=np.float32))
        h_node = self.g.make_const(utils.make_name("Const"), np.array([rnn_props.hidden_size], dtype=np.float32))
        b_node = rnn_props.batch_size_node
        # Concat in OPSET7 does not support int64.
        tile_shape = make_onnx_node(self.g, "Concat", [num_direction_node.output[0], b_node.output[0], h_node.output[0]], attr={"axis": 0})

        # Tile's repeats must be INT64
        attr = {"to": onnx_pb.TensorProto.INT64}
        tile_shape_int64 = make_onnx_node(self.g, 'Cast', [tile_shape.output[0]], attr)

        const_node = self.g.make_const(utils.make_name("Const"), np.array([[[fill_val]]], dtype=fill_val_dtype))
        tile_node = make_onnx_node(self.g, 'Tile', [const_node.output[0], tile_shape_int64.output[0]])
        self.all_nodes.extend([tile_shape, tile_shape_int64, tile_node])
        return tile_node

    def process_seq_length(self, rnn_props, seq_length_node):
        # output: [time step, batch size, input size]
        shape_node = make_onnx_node(self.g, "Shape", [rnn_props.x_input_id])

        # LSTMCell only allow inputs of [batch size, input_size], so we assume dynamic_rnn has 3 dims.
        # Slice cannot support Int64 in OPSET 7, so we cast here.
        attr = {"to": onnx_pb.TensorProto.FLOAT}
        cast_shape_node = make_onnx_node(self.g, "Cast", [shape_node.output[0]], attr)
        self.g.copy_shape(shape_node.output[0], cast_shape_node.output[0])

        attr = {"axes": [0], "starts": [1], "ends": [2]}
        batchsize_node = make_onnx_node(self.g, "Slice", [cast_shape_node.output[0]], attr)

        # Tile's repeats must be INT64
        attr = {"to": onnx_pb.TensorProto.INT64}
        repeat_node = make_onnx_node(self.g, 'Cast', [batchsize_node.output[0]], attr)

        self.all_nodes.extend([shape_node, cast_shape_node, batchsize_node, repeat_node])

        if not seq_length_node:
            attr = {"axes" : [0], "starts": [0], "ends": [1]}
            timestep_node = make_onnx_node(self.g, 'Slice', [cast_shape_node.output[0]], attr)

            tile_node = make_onnx_node(self.g, 'Tile', [timestep_node.output[0], repeat_node.output[0]])

            attr = {"to": onnx_pb.TensorProto.INT32}  # LSTM sequence_lens needs to be int32
            seq_length_node = make_onnx_node(self.g, 'Cast', [tile_node.output[0]], attr)

            self.all_nodes.extend([timestep_node, tile_node, seq_length_node])

        rnn_props.onnx_input_ids["sequence_lens"] = seq_length_node.output[0]
        return seq_length_node, batchsize_node

    def create_rnn_node(self, rnn_props):
        # specify if the RNN is forward, reverse, or bidirectional.
        # Must be one of forward (default), reverse, or bidirectional.
        # Here we won't mark bidirectional/reverse, we will have another rewriter running after this one, which will based 
        # on patterns to combine a forward LSTM and a backward LSTM into a bidirectional one.
        direction = "forward"
        num_direction = 1
        # todo: input_forget
        attr = {"direction": direction, "hidden_size": rnn_props.hidden_size}
        inputs = rnn_props.onnx_input_ids
        lstm_inputs = [
            inputs["X"], inputs["W"], inputs["R"], inputs["B"],
            inputs["sequence_lens"], inputs["initial_h"], inputs["initial_c"]]
        lstm_node = make_onnx_node(self.g, "LSTM", lstm_inputs, attr, 3)

        x_shape = self.g.get_shape(lstm_node.input[0])
        x_seq_length = x_shape[0] 
        x_batch_size = x_shape[1] 
        self.g.set_shape(lstm_node.output[0], [x_seq_length, num_direction, x_batch_size, rnn_props.hidden_size]) 
        self.g.set_shape(lstm_node.output[1], [num_direction, x_batch_size, rnn_props.hidden_size]) 
        self.g.copy_shape(lstm_node.output[1], lstm_node.output[2])
        return lstm_node

    def _connect_lstm_yh_to_graph(self, lstm_node, exit_node, rnn_props):
        # in tf, y_h output shape is: [batch, hidden]
        # in onnx, output shape is: [number_directions, batch, hidden]
        output_id = lstm_node.output[1]
        squeeze_node = make_onnx_node(self.g, "Squeeze", [output_id], attr={"axes": [0]})
        lstm_yh_shape = self.g.get_shape(output_id)
        self.g.set_shape(squeeze_node.output[0], [lstm_yh_shape[1], lstm_yh_shape[2]])
        self.all_nodes.extend([squeeze_node])
        self.g.replace_all_inputs(self.all_nodes, exit_node.output[0], squeeze_node.output[0])

    def _connect_lstm_yc_to_graph(self, lstm_node, exit_node, rnn_props):
        # in tf, y_c output shape is: [batch, hidden]
        # in onnx, output shape is: [number_directions, batch, hidden]
        output_id = lstm_node.output[2]
        squeeze_node = make_onnx_node(self.g, "Squeeze", [output_id], attr={"axes": [0]})
        lstm_yc_shape = self.g.get_shape(output_id)
        self.g.set_shape(squeeze_node.output[0], [lstm_yc_shape[1], lstm_yc_shape[2]])
        self.all_nodes.extend([squeeze_node])
        self.g.replace_all_inputs(self.all_nodes, exit_node.output[0], squeeze_node.output[0])

    def _connect_lstm_ych_to_graph(self, lstm_node, exit_node, rnn_props):
        # in tf, concat of y_c and y_h output shape is: [batch, hidden *2]
        # in onnx, y_c/y_h output shape is: [number_directions, batch, hidden]

        concat = make_onnx_node(self.g, "Concat", [lstm_node.output[2], lstm_node.output[1]], attr={"axis": 2})
        yc_shape = self.g.get_shape(lstm_node.output[2])
        self.g.set_shape(concat.output[0], [yc_shape[0], yc_shape[1], yc_shape[2] * 2])

        squeeze_node = make_onnx_node(self.g, "Squeeze", [concat.output[0]], attr={"axes": [0]})
        concat_shape = self.g.get_shape(concat.output[0])
        self.g.set_shape(squeeze_node.output[0], [concat_shape[1], concat_shape[2]])
        self.all_nodes.extend([concat, squeeze_node])

        self.g.replace_all_inputs(self.all_nodes, exit_node.output[0], squeeze_node.output[0])

    def _connect_lstm_output_to_graph(self, lstm_node, exit_node, rnn_props):
        exit_consumers = self.g.find_output_consumers(exit_node.output[0])
        gather_node = self._validate_output_exit_consumers(exit_consumers)
        if len(exit_consumers) != 2 or not gather_node:
            log.debug("lstm output exit node has " + str(len(exit_consumers)) + " consumers")
            raise ValueError("lstm output exit node check failed")

        # gather output for sure has shape [time, batch, hidden]
        gather_output_id = gather_node.output[0]
        log.debug("found output ta gather node " + gather_output_id)
        # in tf batch major mode, output shape is : [batch, time, hidden]
        # in time major mode, output shape is: [time, batch, hidden]
        # in onnx, output shape is : [time, num_directions, batch, hidden]

        output_id = lstm_node.output[0]
        squeeze_node = make_onnx_node(self.g, "Squeeze", [output_id], attr={"axes": [1]})
        lstm_output_shape = self.g.get_shape(output_id)
        self.g.set_shape(squeeze_node.output[0], [lstm_output_shape[0], lstm_output_shape[2], lstm_output_shape[3]])

        if not rnn_props.time_major:
            gather_consumers = self.g.find_output_consumers(gather_output_id)
            print(gather_consumers)
            gather_trans_consumers = [n for n in gather_consumers if check_is_timemajor_transpose(n)]
            if len(gather_trans_consumers) != 1:
                raise ValueError("batch major should expect a transpose after gather")
            trans = gather_trans_consumers[0] # trans has rnn scope name

            # we just check the transpose here, but will not re-use it, because
            # it may hold non-const perms. so we re-create a new transpose to replace it
            attr = { "perm": np.array([1, 0, 2], dtype=np.int64) }
            new_trans = make_onnx_node(self.g, "Transpose", [squeeze_node.output[0]], attr)
            trans_input_shape = self.g.get_shape(squeeze_node.output[0])
            self.g.replace_all_inputs(self.all_nodes, trans.output[0], new_trans.output[0])
            self.g.set_shape(new_trans.output[0], [trans_input_shape[1], trans_input_shape[0], trans_input_shape[2]])
            self.all_nodes.extend([new_trans])

        self.g.replace_all_inputs(self.all_nodes, gather_output_id, squeeze_node.output[0])
        self.all_nodes.extend([squeeze_node])

    def _validate_output_exit_consumers(self, exit_consumers):
        if len(exit_consumers) != 2:
            return None
        
        gather_node = None
        for n in exit_consumers:
            if n.type == "TensorArrayGatherV3":
                gather_node = n
            elif n.type == "TensorArraySizeV3":
                continue
            else:
                return None

        return gather_node

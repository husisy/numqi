import numpy as np
import pytest

import numpyqi

try:
    import torch
    import torch_wrapper
    from _circuit_torch_utils import DummyQNNModel
except ImportError:
    torch = None
    torch_wrapper = None
    DummyQNNModel = None

np_rng = np.random.default_rng()

def build_dummy_circuit(num_depth, num_qubit):
    circ = numpyqi.sim.Circuit(default_requires_grad=True)
    for ind0 in range(num_depth):
        tmp0 = list(range(0, num_qubit-1, 2)) + list(range(1, num_qubit-1, 2))
        for ind1 in tmp0:
            circ.ry(ind1)
            circ.ry(ind1+1)
            circ.rz(ind1)
            circ.rz(ind1+1)
            circ.cnot(ind1, ind1+1)
            circ.double_qubit_gate(numpyqi.random.rand_haar_unitary(4,4), ind1, ind1+1)
    return circ


@pytest.mark.skipif(torch is None, reason='pytorch is not installed')
def test_dummy_circuit():
    num_qubit = 5
    num_depth = 3
    circuit = build_dummy_circuit(num_depth, num_qubit)
    model = DummyQNNModel(circuit)
    torch_wrapper.check_model_gradient(model)


def test_circuit_to_unitary():
    num_qubit = 5
    circ = build_dummy_circuit(num_depth=3, num_qubit=num_qubit)
    np0 = numpyqi.random.rand_haar_state(2**num_qubit)

    ret_ = circ.apply_state(np0)

    unitary_matrix = circ.to_unitary()
    ret0 = unitary_matrix @ np0
    assert np.abs(unitary_matrix @ unitary_matrix.T.conj() - np.eye(2**num_qubit)).max() < 1e-7
    assert np.abs(ret_-ret0).max() < 1e-7


def test_measure_gate():
    # bell state
    circ = numpyqi.sim.Circuit(default_requires_grad=False)
    circ.H(0)
    circ.cnot(0, 1)
    gate_measure = circ.measure(index=(0,1))
    q0 = numpyqi.sim.state.new_base(num_qubit=2)
    for _ in range(10): #randomness in measure gate, so we repeat here
        q1 = circ.apply_state(q0)
        assert tuple(gate_measure.bitstr) in {(0,0),(1,1)}
        assert np.abs(gate_measure.probability-np.abs([0.5,0,0,0.5])).max() < 1e-7
        if tuple(gate_measure.bitstr)==(0,0):
            assert np.abs(q1-np.array([1,0,0,0])).max() < 1e-7
        else:
            assert np.abs(q1-np.array([0,0,0,1])).max() < 1e-7

    # GHZ state
    circ = numpyqi.sim.Circuit(default_requires_grad=False)
    circ.H(0)
    circ.cnot(0, 1)
    circ.cnot(1, 2)
    gate_measure = circ.measure(index=(0,1,2))
    q0 = numpyqi.sim.state.new_base(num_qubit=3)
    for _ in range(10):
        q1 = circ.apply_state(q0)
        assert tuple(gate_measure.bitstr) in {(0,0,0),(1,1,1)}
        assert np.abs(gate_measure.probability-np.abs([0.5,0,0,0,0,0,0,0.5])).max() < 1e-7
        if tuple(gate_measure.bitstr)==(0,0,0):
            assert np.abs(q1-np.array([1,0,0,0,0,0,0,0])).max() < 1e-7
        else:
            assert np.abs(q1-np.array([0,0,0,0,0,0,0,1])).max() < 1e-7


def hf_ry_rx(alpha, beta):
    r'''
    ry(beta) * rx(alpha)
    '''
    if isinstance(alpha, torch.Tensor):
        assert isinstance(beta, torch.Tensor)
        assert alpha.dtype==beta.dtype
        if alpha.dtype==torch.float32:
            alpha = alpha*torch.tensor(1, dtype=torch.complex64)
            beta = beta*torch.tensor(1, dtype=torch.complex64)
        else:
            assert alpha.dtype==torch.float64
            alpha = alpha*torch.tensor(1, dtype=torch.complex128)
            beta = beta*torch.tensor(1, dtype=torch.complex128)
        cosa,sina,cosb,sinb = torch.cos(alpha/2),torch.sin(alpha/2),torch.cos(beta/2),torch.sin(beta/2)
        cc,cs,sc,ss = cosa*cosb,cosa*sinb,sina*cosb,sina*sinb
        ret = torch.stack([cc+1j*ss,-1j*sc-cs,cs-1j*sc,cc-1j*ss], dim=-1).view(*alpha.shape, 2, 2)
    else:
        alpha = np.asarray(alpha)
        beta = np.asarray(beta)
        # assert alpha.ndim<=1 and beta.ndim<=1
        cosa,sina,cosb,sinb = np.cos(alpha/2),np.sin(alpha/2),np.cos(beta/2),np.sin(beta/2)
        cc,cs,sc,ss = cosa*cosb,cosa*sinb,sina*cosb,sina*sinb
        ret = np.stack([cc+1j*ss,-1j*sc-cs,cs-1j*sc,cc-1j*ss], axis=-1).reshape(*alpha.shape, 2, 2)
    return ret


class RyRxGate(numpyqi.sim.ParameterGate):
    def __init__(self, index, alpha=0, beta=0, requires_grad=True):
        super().__init__(kind='unitary', hf0=hf_ry_rx, args=(alpha,beta), name='ry_rx', requires_grad=requires_grad)
        self.index = index, #must be tuple of int


def test_custom_gate_without_torch():
    alpha,beta = np_rng.uniform(0, 2*np.pi, 2)
    tmp0 = np_rng.uniform(size=2) + 1j*np_rng.uniform(size=2)
    q0 = tmp0 / np.linalg.norm(tmp0)

    circ = numpyqi.sim.Circuit(default_requires_grad=False)
    circ.register_custom_gate('ry_rx', RyRxGate)
    circ.ry_rx(0, alpha, beta)
    q1 = circ.apply_state(q0)

    q2 = numpyqi.gate.ry(beta) @ (numpyqi.gate.rx(alpha) @ q0)
    assert np.abs(q1-q2).max() < 1e-10


@pytest.mark.skipif(torch is None, reason='pytorch is not installed')
def test_custom_gate_with_torch():
    alpha,beta = np_rng.uniform(0, 2*np.pi, 2)
    circ = numpyqi.sim.Circuit(default_requires_grad=False)
    circ.register_custom_gate('ry_rx', RyRxGate)
    circ.ry_rx(0, alpha, beta)
    model = DummyQNNModel(circ)
    torch_wrapper.check_model_gradient(model)
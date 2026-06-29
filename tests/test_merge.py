import pytest
import merge

def seg(coords): return {"coords": coords}

A = seg([[0.0, 0.0], [1.0, 1.0]])
B = seg([[1.0, 1.0], [2.0, 2.0]])
C = seg([[2.0, 2.0], [3.0, 3.0]])

def test_orders_two_in_chain_order_regardless_of_input_order():
    out = merge.order_chain([B, A])
    assert out == [A, B]

def test_orders_three():
    out = merge.order_chain([C, A, B])
    assert out == [A, B, C]

def test_merge_coords_dedups_junction():
    assert merge.merge_coords([A, B, C]) == [[0.0,0.0],[1.0,1.0],[2.0,2.0],[3.0,3.0]]

def test_rejects_fewer_than_two():
    with pytest.raises(merge.MergeError):
        merge.order_chain([A])

def test_rejects_gap():
    far = seg([[5.0, 5.0], [6.0, 6.0]])
    with pytest.raises(merge.MergeError):
        merge.order_chain([A, far])

def test_rejects_anti_parallel_twin():
    rev = seg([[1.0, 1.0], [0.0, 0.0]])   # reverse of A
    with pytest.raises(merge.MergeError):
        merge.order_chain([A, rev])

def test_rejects_branch():
    # A ends at (1,1); both B and B2 start at (1,1) -> branch
    b2 = seg([[1.0, 1.0], [2.0, 9.0]])
    with pytest.raises(merge.MergeError):
        merge.order_chain([A, B, b2])

def test_tolerance_allows_near_join():
    near = seg([[1.00005, 1.00005], [2.0, 2.0]])  # within 1e-4 of A's end
    out = merge.order_chain([A, near])
    assert out == [A, near]

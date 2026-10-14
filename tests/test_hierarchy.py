"""Module coupling report (MODULE_HIERARCHY docs): anchors that a wrong owner or default filter would break."""

from remind_rag import hierarchy


def test_self_test_anchors():
    hierarchy.self_test(hierarchy.collect())


def test_solver_start_values_are_not_writes():
    # 80_optimization loads vm_cons.l from the input gdx; that does not make it a writer of vm_cons
    a = hierarchy.analyse(hierarchy.collect(), 0)
    assert "80_optimization" not in a["writers"]["vm_cons"]
    assert "41_emicapregi" in a["writers"]["vm_Xport"]  # none/bounds.gms fixes permit trade to 0: a real write

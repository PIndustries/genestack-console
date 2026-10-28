import pytest


def test_compute_module_selection_requires_real_cpu_virtualization():
    from app.services.talos_compute import from_cpuinfo

    assert from_cpuinfo("vendor_id : GenuineIntel\nflags : fpu vmx lm\n") == "kvm_intel"
    assert from_cpuinfo("vendor_id : AuthenticAMD\nflags : fpu svm lm\n") == "kvm_amd"
    with pytest.raises(ValueError, match="hardware virtualization"):
        from_cpuinfo("vendor_id : GenuineIntel\nflags : fpu lm\n")


def test_module_configuration_is_host_specific_and_preserves_saved_identity():
    from app.services.talos_compute import configure, documents

    doc = {
        "servers": {"compute": {"roles": ["compute"]}, "storage": {"roles": ["storage"]}},
        "talos": {},
    }
    configured = configure(doc, "compute", "vendor_id: GenuineIntel\nflags: vmx\n")
    assert doc["talos"] == {}
    assert [
        d["name"] for d in documents("compute", configured["talos"], configured["servers"])
    ] == ["kvm", "kvm_intel"]
    assert documents("storage", configured["talos"], configured["servers"]) == []
    with pytest.raises(ValueError, match="differs"):
        configure(configured, "compute", "vendor_id: AuthenticAMD\nflags: svm\n")

"""Testes unitários do módulo de perfilamento de hardware para a GTX 1050 Ti."""

from src.decision.hardware import (
    build_max_memory_map,
    get_hardware_profile,
    sanitize_torch_dtype,
)


def test_sanitize_torch_dtype_blocks_bfloat16():
    # Verifica se bfloat16 é interceptado e coagido
    sanitized = sanitize_torch_dtype("torch.bfloat16")
    assert "bfloat16" not in str(sanitized).lower()

    # Tipos permitidos devem passar sem alteração
    assert sanitize_torch_dtype("float16") == "float16"
    assert sanitize_torch_dtype("float32") == "float32"


def test_get_hardware_profile_returns_valid_object():
    profile = get_hardware_profile()
    assert profile is not None
    assert isinstance(profile.has_cuda, bool)
    assert isinstance(profile.device_name, str)
    assert profile.recommended_max_length in (512, 1024, 2048)
    assert profile.recommended_dtype in ("float16", "float32")


def test_build_max_memory_map():
    mem_map = build_max_memory_map(max_vram_mb=3000, cpu_ram_gb=12)
    assert mem_map[0] == "3000MiB"
    assert mem_map["cpu"] == "12GiB"

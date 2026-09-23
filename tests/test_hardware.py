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
    """Perfil deve ser derivado do ambiente REAL (torch) com invariantes coerentes."""
    import torch

    profile = get_hardware_profile()

    # Coerência com o torch real do ambiente
    assert profile.has_cuda is torch.cuda.is_available()

    assert isinstance(profile.device_name, str)
    assert profile.recommended_max_length in (512, 1024, 2048)
    assert profile.recommended_dtype in ("float16", "float32")
    assert profile.recommended_device in ("cpu", "cuda:0")

    if not profile.has_cuda:
        # Caminho CPU: invariantes fixas do fallback
        assert profile.device_name == "CPU"
        assert profile.total_vram_mb == 0
        assert profile.free_vram_mb == 0
        assert profile.supports_bfloat16 is False
        assert profile.recommended_dtype == "float32"
        assert profile.recommended_device == "cpu"
        assert profile.recommended_max_length == 512
        assert profile.compute_capability == (0, 0)
        assert profile.is_pascal_or_older is False
    else:
        major, minor = profile.compute_capability
        # Capacidade de compute espelha o torch
        assert (major, minor) == torch.cuda.get_device_capability(0)
        assert profile.device_name == torch.cuda.get_device_name(0)
        assert profile.device_name.strip() != ""
        # Memória coerente
        assert 0 <= profile.free_vram_mb <= profile.total_vram_mb
        # Regras de arquitetura documentadas
        assert profile.is_pascal_or_older is (major <= 6)
        assert profile.supports_bfloat16 is (major >= 8)
        # Política de fallback por VRAM livre
        if profile.free_vram_mb < 800:
            assert profile.recommended_device == "cpu"
            assert profile.recommended_dtype == "float32"
            assert profile.recommended_max_length == 512
        else:
            assert profile.recommended_device == "cuda:0"
            assert profile.recommended_dtype == "float16"
            assert profile.recommended_max_length == 1024


def test_build_max_memory_map():
    mem_map = build_max_memory_map(max_vram_mb=3000, cpu_ram_gb=12)
    assert mem_map[0] == "3000MiB"
    assert mem_map["cpu"] == "12GiB"

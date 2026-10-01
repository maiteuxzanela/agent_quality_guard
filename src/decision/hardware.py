"""Módulo de Perfilamento e Guardrails de Hardware.
Otimizado especificamente para NVIDIA GeForce GTX 1050 Ti (4GB VRAM, Pascal sm_61) em Linux Pop!_OS.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from ..core.schemas import HardwareUnavailableError

logger = logging.getLogger("jev.hardware")


@dataclass
class HardwareProfile:
    """Perfil detalhado de hardware da máquina local."""
    has_cuda: bool
    device_name: str
    compute_capability: Tuple[int, int]
    is_pascal_or_older: bool
    total_vram_mb: int
    free_vram_mb: int
    supports_bfloat16: bool
    recommended_dtype: str
    recommended_device: str
    recommended_max_length: int


def sanitize_torch_dtype(requested_dtype: Any) -> Any:
    """Garante que bfloat16 NUNCA seja utilizado em GPUs Pascal (GTX 1050 Ti).
    Coage para float16 ou float32 conforme a capacidade do hardware.
    """
    dtype_str = str(requested_dtype).lower()
    if "bfloat16" in dtype_str:
        logger.warning(
            "ALERTA DE HARDWARE: Tentativa de uso de 'bfloat16' detectada. "
            "A arquitetura Pascal GP107 (GTX 1050 Ti) NÃO possui suporte a bfloat16. "
            "Coagindo automaticamente para 'float16' para evitar falha catastrófica de runtime CUDA."
        )
        try:
            import torch
            return torch.float16
        except ImportError:
            return "float16"
    return requested_dtype


import fcntl

@dataclass
class _GpuLockState:
    """Estado mutável do lock exclusivo de GPU (holder explícito, sem 'global')."""

    handle: Optional[Any] = None


_GPU_LOCK = _GpuLockState()


def try_acquire_gpu_lock() -> bool:
    """Tenta adquirir lock exclusivo na GPU para evitar que múltiplos processos saturem os 4GB de VRAM.

    Exemplo:
        >>> try_acquire_gpu_lock() in (True, False)
        True
    """
    if _GPU_LOCK.handle is not None:
        return True
    handle: Optional[Any] = None
    try:
        handle = open("/tmp/openjev_gpu.lock", "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _GPU_LOCK.handle = handle
        return True
    except (BlockingIOError, OSError):
        if handle is not None:
            try:
                handle.close()
            except Exception as close_err:
                logger.debug(f"Falha ao fechar arquivo de lock da GPU: {close_err}")
        return False


def get_hardware_profile() -> HardwareProfile:
    """Inspecciona o ambiente de execução e infere limites seguros de memória e precisão."""
    try:
        import torch
    except ImportError:
        logger.warning("PyTorch não encontrado no ambiente Python ativo.")
        return HardwareProfile(
            has_cuda=False,
            device_name="CPU (PyTorch ausente)",
            compute_capability=(0, 0),
            is_pascal_or_older=False,
            total_vram_mb=0,
            free_vram_mb=0,
            supports_bfloat16=False,
            recommended_dtype="float32",
            recommended_device="cpu",
            recommended_max_length=1024,
        )

    has_cuda = torch.cuda.is_available()
    if not has_cuda:
        logger.info("Nenhuma GPU CUDA disponível detectada. Operando estritamente em CPU.")
        return HardwareProfile(
            has_cuda=False,
            device_name="CPU",
            compute_capability=(0, 0),
            is_pascal_or_older=False,
            total_vram_mb=0,
            free_vram_mb=0,
            supports_bfloat16=False,
            recommended_dtype="float32",
            recommended_device="cpu",
            recommended_max_length=512,
        )

    device_name = torch.cuda.get_device_name(0)
    major, minor = torch.cuda.get_device_capability(0)
    compute_capability = (major, minor)

    # Pascal é sm_60 e sm_61 (GTX 1050 Ti é sm_61)
    is_pascal_or_older = major <= 6

    # bfloat16 requer nativamente Ampere (sm_80+) ou superior
    supports_bfloat16 = major >= 8

    # VRAM em Megabytes
    try:
        free_bytes, total_bytes = torch.cuda.mem_get_info(0)
        free_mb = int(free_bytes / (1024 * 1024))
        total_mb = int(total_bytes / (1024 * 1024))
    except Exception as e:
        logger.warning(f"Não foi possível obter mem_get_info da GPU: {e}")
        free_mb = 0
        total_mb = 4096

    # Trava de Concorrência da GPU: Uma GTX 1050 Ti (4GB) suporta apenas UMA instância do Laya na VRAM.
    gpu_lock_acquired = try_acquire_gpu_lock()
    if not gpu_lock_acquired:
        logger.info(
            "Outro processo já possui lock exclusivo da GPU GTX 1050 Ti. "
            "Roteando esta instância de forma segura para CPU para preservar VRAM e estabilidade."
        )
        return HardwareProfile(
            has_cuda=True,
            device_name=device_name,
            compute_capability=compute_capability,
            is_pascal_or_older=is_pascal_or_older,
            total_vram_mb=total_mb,
            free_vram_mb=free_mb,
            supports_bfloat16=supports_bfloat16,
            recommended_dtype="float32",
            recommended_device="cpu",
            recommended_max_length=512,
        )

    # Na 1050 Ti (4GB): o modelo Laya ocupa ~1.6 GB. Exigimos pelo menos 2.2 GB livres
    # para garantir que o compositor Wayland (COSMIC) e as janelas da IDE mantenham
    # pelo menos 600-800 MB de VRAM dedicados para renderização sem glitches.
    if free_mb < 2200:
        recommended_device = "cpu"
        recommended_dtype = "float32"
        recommended_max_length = 512
        logger.warning(
            f"VRAM livre na GPU ({free_mb} MB < 2200 MB) insuficiente para operar com segurança "
            "sem asfixiar o compositor desktop. Ativando fallback seguro para CPU."
        )
    else:
        recommended_device = "cuda:0"
        # Pascal tem armazenamento em float16 que reduz a pegada de VRAM pela metade
        recommended_dtype = "float16"
        recommended_max_length = 1024

    return HardwareProfile(
        has_cuda=True,
        device_name=device_name,
        compute_capability=compute_capability,
        is_pascal_or_older=is_pascal_or_older,
        total_vram_mb=total_mb,
        free_vram_mb=free_mb,
        supports_bfloat16=supports_bfloat16,
        recommended_dtype=recommended_dtype,
        recommended_device=recommended_device,
        recommended_max_length=recommended_max_length,
    )


def build_max_memory_map(max_vram_mb: int = 3000, cpu_ram_gb: int = 12) -> Dict[Any, str]:
    """Cria um mapa de memória seguro para offloading via Accelerate/Transformers."""
    return {
        0: f"{max_vram_mb}MiB",
        "cpu": f"{cpu_ram_gb}GiB",
    }


def clean_gpu_memory() -> None:
    """Libera caches residuais da VRAM.

    Exemplo:
        >>> clean_gpu_memory() is None
        True
    """
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception as e:
        logger.debug(f"Falha ao limpar memória da GPU: {e}")

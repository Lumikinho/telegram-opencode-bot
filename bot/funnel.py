"""Tailscale funnel: status, ativar e desativar os mapeamentos do servidor.

Mapeamentos padrao (restaurados pelo "ativar"):
  /   -> dashboard Caddy (:8888)
  /ai -> opencode server (:8081)
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

FUNNEL_TARGETS: tuple[tuple[str, str], ...] = (
    ("/", "http://127.0.0.1:8888"),
    ("/ai", "http://127.0.0.1:8081"),
)


async def _run_shell(cmd: str, timeout: float = 15.0) -> tuple[str, str, int | None]:
    """Roda um comando shell sem bloquear o loop. Retorna (stdout, stderr, returncode)."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return "", "shell indisponível", None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "", "timeout", None
    return out.decode(errors="replace").strip(), err.decode(errors="replace").strip(), proc.returncode


async def get_funnel_status() -> str:
    out, _err, _rc = await _run_shell("tailscale funnel status")
    return out


def parse_active_funnels(status_output: str) -> list[dict]:
    """Agrupa a saida do `tailscale funnel status` por URL.

    Uma entrada nova comeca na linha da URL publica; linhas recuadas
    (`|-- ...`) sao mapeamentos da entrada acima.
    """
    funnels: list[dict] = []
    if not status_output:
        return funnels
    for raw_line in status_output.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("https://", "http://")):
            url = stripped.split(" ", 1)[0]
            funnels.append({
                "url": url,
                "on": "funnel on" in stripped.lower(),
                "mappings": [],
            })
            continue
        if funnels:
            mapping = stripped.lstrip("|-").strip()
            if mapping:
                funnels[-1]["mappings"].append(mapping)
    return funnels


async def funnel_off() -> tuple[bool, str]:
    """Desativa tudo (`tailscale funnel reset`)."""
    _out, err, rc = await _run_shell("tailscale funnel reset")
    if rc == 0:
        logger.info("Funnel desativado")
        return True, "Funnel desativado."
    logger.warning("Falha ao desativar funnel: %s", err)
    return False, f"Falha ao desativar: `{err or 'erro desconhecido'}`"


async def funnel_on() -> tuple[bool, str]:
    """Reativa os mapeamentos padrao (dashboard + opencode)."""
    await _run_shell("tailscale funnel reset")
    ok_all, msgs = True, []
    for path, target in FUNNEL_TARGETS:
        _out, err, rc = await _run_shell(
            f"tailscale funnel --bg --set-path={path} {target}")
        if rc == 0:
            msgs.append(f"`{path}` -> `{target}`")
        else:
            ok_all = False
            msgs.append(f"`{path}` falhou: `{err or 'erro desconhecido'}`")
    if ok_all:
        logger.info("Funnel ativado: %s", ", ".join(msgs))
        return True, "Funnel ativado:\n" + "\n".join(msgs)
    logger.warning("Funnel parcial: %s", "; ".join(msgs))
    return False, "Ativado parcialmente:\n" + "\n".join(msgs)

#!/usr/bin/env python3
"""Worker do funnel Tailscale: stdin JSON -> stdout JSON. Sem Telegram.

Ações: {"action": "status"} -> {"raw", "funnels"}
       {"action": "on"}     -> {"ok", "message"}
       {"action": "off"}    -> {"ok", "message"}
"""
import asyncio
import json
import sys

FUNNEL_TARGETS = (
    # Rota única: o Caddy (:8888) distribui dashboard (/), API (/api),
    # opencode serve (/ai), opencode web (/web), code-server (/code)...,
    # então o funnel não precisa de um --set-path por serviço.
    ("/", "http://127.0.0.1:8888"),
)


async def _run_shell(cmd: str, timeout: float = 15.0):
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
    return (out.decode(errors="replace").strip(),
            err.decode(errors="replace").strip(), proc.returncode)


def parse_active_funnels(status_output: str) -> list:
    funnels: list = []
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


async def _status() -> dict:
    out, _err, _rc = await _run_shell("tailscale funnel status")
    return {"raw": out, "funnels": parse_active_funnels(out)}


async def _off() -> dict:
    _out, err, rc = await _run_shell("tailscale funnel reset")
    if rc == 0:
        return {"ok": True, "message": "Funnel desativado."}
    return {"ok": False, "message": f"Falha ao desativar: `{err or 'erro desconhecido'}`"}


async def _on() -> dict:
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
        return {"ok": True, "message": "Funnel ativado:\n" + "\n".join(msgs)}
    return {"ok": False, "message": "Ativado parcialmente:\n" + "\n".join(msgs)}


async def amain() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    action = (payload.get("action") or "status").lower()
    if action == "on":
        result = await _on()
    elif action == "off":
        result = await _off()
    else:
        result = await _status()
    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    asyncio.run(amain())

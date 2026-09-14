# Changelog — bot híbrido (Bun+TS+Python)

## [2.0.0-hybrid.1] - 2026-09-14

- Turnos completos no gateway: status com throttle, streaming, resposta-em-cima/think-embaixo
- Permissões e perguntas (forms v2) por botões e texto livre
- Anexos: foto, documento, áudio, voz, vídeo e GIF (limite 20 MiB)
- `/restart [bot|servidor|ambos]` com confirmação e kill com teto
- Persistência `bun:sqlite` (sessões sobrevivem a reinícios)
- Mensagem de boot com versão, branch e este changelog

## [2.0.0-hybrid.0] - 2026-09-14

- Esqueleto híbrido: gateway Bun+TS (grammy) + workers Python via JSON
- Cliente HTTP do opencode server v2 (BasicAuth, `/api/*`, SSE)
- Workers de bateria, funnel e render; `run-hybrid.sh` e `HYBRID.md`

/** Tratamento de erros: traduz exceções técnicas em mensagens PT-BR acionáveis.
 *
 * Todo erro que chega ao Telegram passa por `friendlyError` — nunca vaza
 * JSON cru (`{'type': 'provider.internal', ...}`) para o chat.
 */
export function friendlyError(e: unknown): string {
  const raw = e instanceof Error
    ? `${e.name}: ${e.message}`
    : typeof e === "object"
      ? JSON.stringify(e) ?? String(e)
      : String(e ?? "");
  const s = raw.slice(0, 500);

  if (/No payment method/i.test(s)) {
    return "[COST] Provedor recusou: sem método de pagamento no OpenCode Zen. " +
      "Adicione em opencode.ai → billing ou use um modelo gratuito (/models).";
  }
  if (/quota|QuotaExceeded/i.test(s)) {
    return "[COST] Cota do provedor esgotada. Aguarde a janela resetar ou troque de modelo (/models).";
  }
  if (/rate ?limit|429/i.test(s)) {
    return "[T] Limite do provedor atingido. Aguarde ~1 min e reenvie.";
  }
  if (/worker \/api\//i.test(s)) {
    return "[ERR] Worker Python fora do ar ou com erro. Suba com `make dev-worker` e reenvie.";
  }
  if (/provider\.internal|Internal server error|\b500\b/i.test(s)) {
    return "[ERR] Erro interno do provedor (500). Geralmente transitório — " +
      "aguarde ~1 min e reenvie. Se persistir, /new ou troque de modelo (/models).";
  }
  if (/provider\.auth|401|Unauthorized/i.test(s)) {
    return "[DENY] Falha de autenticação no provedor (401). Confira credenciais/billing e tente de novo.";
  }
  if (/Bad Request|\b400\b|invalid_request/i.test(s)) {
    return "[ERR] Pedido rejeitado pelo provedor (400). Encurte a mensagem ou /new e tente de novo.";
  }
  if (/socket .*closed|connection .*closed|ECONNRESET|terminated/i.test(s) && !/HTTP \d/.test(s)) {
    return "[NET] Conexão caiu no meio da chamada (worker/servidor reiniciou?). " +
      "O bot reconecta sozinho — só reenviar.";
  }
  if (/ConnectError|ECONNREFUSED|fetch failed|Failed to fetch|aborted|opencode.*HTTP [45]/i.test(s)) {
    return "[ERR] Servidor opencode inacessível. Veja /status (o bot tenta religar sozinho).";
  }
  if (/Timed out|Timeout|timeout/i.test(s)) {
    return "[T] Operação demorou demais (timeout). Tente de novo; se repetir, divida o pedido.";
  }
  return `[ERR] ${s.slice(0, 300)}`;
}

/** Resposta vazia no fim do turno: o provedor pode ter falhado sem texto. */
export function emptyAnswerHint(): string {
  return "(sem resposta — o provedor pode ter falhado; aguarde e reenvie, ou /new)";
}

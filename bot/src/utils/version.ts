/** Versão, branch e changelog exibidos no boot do bot. */
import { VERSION as CONFIG_VERSION } from "../config/env";

const ROOT = `${import.meta.dir}/../..`;

function runGit(...args: string[]): string {
  try {
    const out = Bun.spawnSync(["git", "-C", ROOT, ...args]);
    if (out.exitCode !== 0) return "";
    return out.stdout.toString().trim();
  } catch {
    return "";
  }
}

export async function getVersion(): Promise<string> {
  try {
    const pkg = (await Bun.file(`${ROOT}/package.json`).json()) as { version?: string };
    if (pkg.version) return pkg.version;
  } catch {
    /* cai no fallback */
  }
  return CONFIG_VERSION;
}

export function getBranch(): string {
  return runGit("rev-parse", "--abbrev-ref", "HEAD") || process.env.HYBRID_BRANCH || "desconhecida";
}

export function getCommit(): string {
  return runGit("rev-parse", "--short", "HEAD");
}

/** Extrai o corpo da seção `## [versão]` do changelog; cai na primeira
 * seção `## ` quando a versão não é encontrada. */
export function extractSection(md: string, version: string): { title: string; body: string } {
  const lines = md.split(/\r?\n/);
  const head = (t: string): boolean => /^##\s+/.test(t);
  const want = lines.findIndex((l) => head(l) && l.includes(version));
  const first = lines.findIndex((l) => head(l));
  const start = want >= 0 ? want : first;
  if (start < 0) return { title: "", body: "" };
  const title = lines[start].replace(/^##\s+/, "").trim();
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    if (head(lines[i])) {
      end = i;
      break;
    }
  }
  return { title, body: lines.slice(start + 1, end).join("\n").trim() };
}

export interface StartupInfo {
  version: string;
  branch: string;
  commit: string;
  changelogTitle: string;
  changelog: string;
}

export async function getStartupInfo(): Promise<StartupInfo> {
  const version = await getVersion();
  let changelogTitle = "";
  let changelog = "";
  try {
    const md = await Bun.file(`${ROOT}/CHANGELOG.md`).text();
    const sec = extractSection(md, version);
    changelogTitle = sec.title;
    changelog = sec.body;
  } catch {
    /* sem changelog: mensagem só com versão/branch */
  }
  return { version, branch: getBranch(), commit: getCommit(), changelogTitle, changelog };
}

/** Monta o texto (markdown) da mensagem de boot. */
export function startupMarkdown(info: StartupInfo): string {
  const head = [`[OK] *opencode bot online*`, ""];
  const commit = info.commit ? ` • commit: \`${info.commit}\`` : "";
  head.push(`versão: \`${info.version}\` • branch: \`${info.branch}\`${commit}`);
  if (info.changelog) {
    head.push("", `*Novidades (${info.changelogTitle || info.version}):*`, "", info.changelog);
  }
  return head.join("\n");
}

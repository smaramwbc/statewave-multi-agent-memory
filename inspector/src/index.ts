import "dotenv/config";

interface Episode {
  id: string;
  subject_id: string;
  source: string;
  type: string;
  payload: Record<string, unknown>;
  metadata: Record<string, unknown>;
  occurred_at: string;
  created_at: string;
}

interface Memory {
  id: string;
  subject_id: string;
  kind: string;
  content: string;
  confidence?: number;
  status?: "active" | "superseded";
  valid_from?: string;
  valid_to?: string;
  created_at: string;
}

interface TimelineResponse {
  subject_id: string;
  episodes: Episode[];
  memories: Memory[];
}

interface AuditEntry {
  timestamp: string;
  agent: string;
  kind: string;
  excerpt: string;
  memoryIdsCited: string[];
  episodeId: string;
  section?: string;
  runNumber?: number;
}

function parseArgs(argv: string[]): { subjectId: string } {
  for (const flag of ["--subject-id", "--run-id"]) {
    const idx = argv.indexOf(flag);
    if (idx !== -1 && argv[idx + 1]) {
      return { subjectId: argv[idx + 1] };
    }
  }
  console.error("Usage: npx tsx src/index.ts --subject-id <id>");
  process.exit(1);
}

async function fetchTimeline(
  baseUrl: string,
  apiKey: string | undefined,
  subjectId: string,
): Promise<TimelineResponse> {
  const url = `${baseUrl}/v1/timeline?subject_id=${encodeURIComponent(subjectId)}`;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (apiKey) headers["X-API-Key"] = apiKey;

  const resp = await fetch(url, { headers });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Statewave ${resp.status}: ${text}`);
  }
  return resp.json() as Promise<TimelineResponse>;
}

function excerptFor(ep: Episode): string {
  const p = ep.payload;
  switch (ep.type) {
    case "agent.researcher.findings": {
      const name = String(p["competitor"] ?? "");
      const pricing = String(p["pricing_model"] ?? "");
      return `${name} — ${pricing}`.substring(0, 120);
    }
    case "agent.critic.evaluation":
    case "agent.analyst.summary": {
      const conf = p["overall_confidence"] ?? p["summary"];
      if (typeof conf === "number") return `Overall confidence: ${conf}`;
      return String(conf ?? "").substring(0, 120);
    }
    case "agent.writer.draft": {
      const content = String(p["content"] ?? p["text"] ?? "");
      return content.substring(0, 120) + (content.length > 120 ? "…" : "");
    }
    case "pipeline.run.completed": {
      const runNum = p["run_number"];
      const stage = p["stage"];
      const comp = p["competitors_written"];
      if (stage) return `Run ${runNum} — ${stage} complete`;
      return `Run ${runNum} checkpoint — ${comp} competitor(s) written`;
    }
    case "pipeline.stage.completed": {
      return `Stage checkpoint: ${p["stage"]}`;
    }
    default:
      return JSON.stringify(p).substring(0, 120);
  }
}

function buildAuditEntries(timeline: TimelineResponse): AuditEntry[] {
  return timeline.episodes.map((ep) => {
    const cited = ep.payload["memory_ids_cited"];
    const runNumber = ep.payload["run_number"] ?? ep.metadata["run_number"];
    return {
      timestamp: ep.created_at ?? ep.occurred_at,
      agent: ep.source,
      kind: ep.type,
      excerpt: excerptFor(ep),
      memoryIdsCited: Array.isArray(cited) ? (cited as string[]) : [],
      episodeId: ep.id,
      section: ep.metadata["section"] as string | undefined,
      runNumber: typeof runNumber === "number" ? runNumber : undefined,
    };
  });
}

function groupByAgent(entries: AuditEntry[]): Map<string, AuditEntry[]> {
  const map = new Map<string, AuditEntry[]>();
  const order: string[] = [];
  for (const e of entries) {
    if (!map.has(e.agent)) {
      map.set(e.agent, []);
      order.push(e.agent);
    }
    map.get(e.agent)!.push(e);
  }
  const ordered = new Map<string, AuditEntry[]>();
  for (const key of order) ordered.set(key, map.get(key)!);
  return ordered;
}

function formatMemoryCited(ids: string[]): string {
  if (ids.length === 0) return "  cited memories: (none — writing phase)";
  const lines = ids.map((id) => `    • ${id}`).join("\n");
  return `  cited memories:\n${lines}`;
}

function bar(label: string, width = 72): string {
  const padded = `── ${label}  `;
  return padded + "─".repeat(Math.max(0, width - padded.length));
}

async function main(): Promise<void> {
  const { subjectId } = parseArgs(process.argv);
  const baseUrl = (process.env["STATEWAVE_URL"] ?? "http://localhost:8100").replace(/\/+$/, "");
  const apiKey = process.env["STATEWAVE_API_KEY"];

  let timeline: TimelineResponse;
  try {
    timeline = await fetchTimeline(baseUrl, apiKey, subjectId);
  } catch (err) {
    console.error(`Failed to fetch timeline: ${(err as Error).message}`);
    process.exit(1);
  }

  const episodeCount = timeline.episodes?.length ?? 0;
  const allMemories = timeline.memories ?? [];
  const activeMemories = allMemories.filter((m) => (m.status ?? "active") === "active");
  const supersededMemories = allMemories.filter((m) => m.status === "superseded");

  console.log("╔" + "═".repeat(72) + "╗");
  console.log("║  Statewave Audit Trail — Cross-Run Intelligence" + " ".repeat(25) + "║");
  console.log(`║  Subject: ${subjectId.substring(0, 50)}` + " ".repeat(Math.max(0, 63 - subjectId.substring(0, 50).length)) + "║");
  console.log(`║  ${episodeCount} episode(s)  ·  ${activeMemories.length} active  ·  ${supersededMemories.length} superseded` + " ".repeat(Math.max(0, 70 - String(episodeCount).length - String(activeMemories.length).length - String(supersededMemories.length).length - 26)) + "║");
  console.log("╚" + "═".repeat(72) + "╝");
  console.log();

  const entries = buildAuditEntries(timeline);
  const grouped = groupByAgent(entries);

  for (const [agent, agentEntries] of grouped) {
    const runNumbers = [...new Set(agentEntries.map((e) => e.runNumber).filter(Boolean))];
    const runLabel = runNumbers.length > 0 ? ` across ${runNumbers.length} run(s)` : "";
    console.log(bar(`${agent.toUpperCase()}  (${agentEntries.length} episode(s)${runLabel})`));
    console.log();

    let lastRun: number | undefined;
    for (const e of agentEntries) {
      if (e.runNumber !== undefined && e.runNumber !== lastRun) {
        lastRun = e.runNumber;
        console.log(`  ── Run ${e.runNumber} ──`);
      }
      const ts = new Date(e.timestamp).toISOString();
      const sectionTag = e.section ? `  [section: ${e.section}]` : "";
      console.log(`  ${ts}  │  ${e.kind}${sectionTag}`);
      console.log(`  ep: ${e.episodeId.substring(0, 8)}…`);
      console.log(`  ${e.excerpt}`);
      console.log(formatMemoryCited(e.memoryIdsCited));
      console.log();
    }
  }

  // Active memories
  console.log(bar(`ACTIVE MEMORIES  (${activeMemories.length} — what agents see now)`));
  console.log();
  if (activeMemories.length === 0) {
    console.log("  (none — run the pipeline first)");
  } else {
    for (const m of activeMemories) {
      const conf = m.confidence != null ? m.confidence.toFixed(2) : "—   ";
      const kind = (m.kind ?? "memory").padEnd(16);
      const preview = (m.content ?? "").substring(0, 80);
      console.log(`  [${kind}  conf:${conf}]  ${preview}`);
    }
  }
  console.log();

  // Superseded memories — the proof that conflict resolution ran
  if (supersededMemories.length > 0) {
    console.log(bar(`SUPERSEDED MEMORIES  (${supersededMemories.length} — overwritten by newer intel)`));
    console.log();
    for (const m of supersededMemories) {
      const kind = (m.kind ?? "memory").padEnd(16);
      const preview = (m.content ?? "").substring(0, 80);
      const validTo = m.valid_to ? new Date(m.valid_to).toISOString() : "unknown";
      console.log(`  [SUPERSEDED  ${kind}]  ${preview}`);
      console.log(`  └─ replaced at: ${validTo}`);
      console.log();
    }
    console.log(`  ↳ These memories existed but were automatically superseded when newer`);
    console.log(`    contradicting intelligence was compiled. Agents never see them.`);
    console.log();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

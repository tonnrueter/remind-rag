"""remind-rag: retrieval over the REMIND source (GAMS, R, docs, scenario configs), served as an MCP server.

The modules form four groups. Each group imports only from the groups above it (one exception: enrich borrows
render.short to cut text), so the list doubles as a reading order.

Reading REMIND's sources (no index needed)
    gams           GAMS line by line: comments, statement ends, $ifthen conditions, run-time if() blocks
    usage          how a symbol is used on a line (role), switch tests, conditions evaluated against switch values
    chunkers       source files -> retrieval chunks, symbols and switches (GAMS, R, Markdown)
    scenarios      config/scenario_config*.csv: scenario rows as chunks, switch resolution (copyConfigFrom)
    gmsdata        the gms/goxygen export: module interfaces, limitations, not_used reasons

Building the index (python -m remind_rag.index)
    checkout       which REMIND commit an index was built from, and which checkout it is served for
    corpus         which REMIND files get indexed
    enrich         plain-English headers per chunk, so the embedding model sees meaning instead of raw GAMS
    embeddings     the local embedding model (bge-small via fastembed): loading, download, fingerprint
    preconditions  which switch settings a realization refuses to run with (stored for get_module; also a report)
    store          the index file: which one to open, how to open it, every table
    index          the build itself: corpus -> chunk -> enrich -> embed -> SQLite, parser checks, --promote

Answering (the MCP tools, python -m remind_rag.server)
    search         hybrid ranking: keyword (BM25) + vector, merged by reciprocal rank fusion
    status         does this code run in a default configuration? the ✓ / ? / ⚠ verdicts
    links          one-hop links between symbols, computed at query time
    render         text formatting of chunks and use lines
    steering       what steers a module's realizations (get_module "Steered by")
    server         the MCP server and its six tools

Reports for humans (run by hand, output goes into workspace docs)
    hierarchy      module coupling measured from the index (MODULE_HIERARCHY)
    doccheck       REMIND's prose checked against facts decidable from the code (STALE_COMMENTS)
"""


def hello() -> str:
    return "Hello from remind-rag!"

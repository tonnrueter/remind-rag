# Export REMIND structure known to gms/goxygen as JSON for the RAG indexer.
#
#   Rscript r/export_gms.R <remind-root> <out.json>
#
# Uses the gms/goxygen versions installed in r/library (install from the local clones with
# `R CMD INSTALL -l r/library ../gms ../goxygen`), never the system library versions.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2) stop("usage: Rscript r/export_gms.R <remind-root> <out.json>")
remindRoot <- normalizePath(args[1], winslash = "/")
outFile <- normalizePath(args[2], winslash = "/", mustWork = FALSE)

scriptDir <- dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(), value = TRUE)),
                                   winslash = "/"))
.libPaths(c(file.path(scriptDir, "library"), .libPaths()))

required <- c(gms = "0.35.0", goxygen = "1.5.1")
for (p in names(required)) {
  if (packageVersion(p) < required[[p]]) {
    stop(p, " ", packageVersion(p), " found, need >= ", required[[p]], " in ", file.path(scriptDir, "library"))
  }
}

setwd(remindRoot)

cc <- suppressMessages(suppressWarnings(gms::codeCheck(details = TRUE, test_switches = FALSE)))

interfaces <- do.call(rbind, lapply(names(cc$interfaceInfo), function(m) {
  x <- cc$interfaceInfo[[m]]
  data.frame(module = m, name = unname(x), direction = names(x), stringsAsFactors = FALSE)
}))

modules <- cc$modulesInfo[, c("name", "number", "folder", "realizations")]

# goxygen doc blocks we care about, per file
docTypes <- c("title", "description", "limitations")
docs <- list()
for (i in seq_len(nrow(modules))) {
  folder <- file.path("modules", modules$folder[i])
  files <- c(file.path(folder, "module.gms"),
             Sys.glob(file.path(folder, strsplit(modules$realizations[i], ",")[[1]], "realization.gms")))
  for (f in files[file.exists(files)]) {
    blocks <- goxygen::extractDocumentation(f)
    for (j in seq_along(blocks)) {
      type <- names(blocks)[j]
      if (!type %in% docTypes) next
      docs[[length(docs) + 1]] <- list(path = f, module = modules$folder[i], type = type,
                                       text = paste(blocks[[j]]$content, collapse = "\n"))
    }
  }
}

# reasons why a realization does not address an interface
notUsed <- list()
for (f in Sys.glob("modules/*/*/not_used.txt")) {
  tab <- tryCatch(utils::read.csv(f, comment.char = "#", stringsAsFactors = FALSE, strip.white = TRUE),
                  error = function(e) NULL)
  if (is.null(tab) || nrow(tab) == 0 || !"name" %in% names(tab)) next
  parts <- strsplit(f, "/", fixed = TRUE)[[1]]
  notUsed[[length(notUsed) + 1]] <- data.frame(module = parts[2], realization = parts[3], name = tab$name,
                                               type = if ("type" %in% names(tab)) tab$type else "",
                                               reason = if ("reason" %in% names(tab)) tab$reason else "",
                                               stringsAsFactors = FALSE)
}
notUsed <- do.call(rbind, notUsed)

out <- list(
  versions = list(gms = as.character(packageVersion("gms")), goxygen = as.character(packageVersion("goxygen")),
                  R = paste(R.version$major, R.version$minor, sep = ".")),
  modules = modules,
  interfaces = interfaces,
  declarations = cc$declarations[, c("names", "sets", "description", "type", "origin")],
  docs = docs,
  not_used = notUsed,
  codecheck_warnings = as.character(attr(cc, "last.warning"))
)
jsonlite::write_json(out, outFile, auto_unbox = TRUE, pretty = TRUE, null = "null", na = "null")
message("wrote ", outFile, ": ", nrow(interfaces), " interface entries, ", length(docs), " doc blocks, ",
        nrow(notUsed), " not_used rows")

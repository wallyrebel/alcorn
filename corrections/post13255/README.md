# Fixed editorial cleanup for Alcorn post13255

Only the five literal '; confirm live price at publish' substrings may be removed.
The managed runner authenticates and validates the fixed published post and all
current rendered field digests, derives the candidate from its current raw body,
and preserves the raw body byte-for-byte except those five deletions. It never
submits title,excerpt,price,metadata,status,date,slug,taxonomy or media updates.

The reviewed rendered candidate digest and literal deletion/count are hard-coded.
Dry-run stores current raw hashes and the exact derived raw payload digest, with
zero writes. Apply requires that successful receipt, unchanged implementation and
ledger, fresh authenticated identity/digest checks and an immediate fixed-operation
read before the single content update.401/403stops; uncertainty reconciles the
same post without automatic retry. Exact after-content is an idempotent no-op.

Only existing repository-managed credentials are used in Actions. The new branch
does not trigger the RSSpublisher. No no-derivatives/republication notice was found
in this publisher-authored guide; no product copy or images are changed. A factual
correction note is unnecessary because only internal editorial instructions are
removed. Preserve all other prose,prices,claims,links and imagery.

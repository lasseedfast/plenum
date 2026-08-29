The full database reference: every column, what the names do not tell you, how the
full-text index works, what the decision values mean, and worked queries.

Call this once before writing anything beyond a simple count — anything involving
$proposal_plural and their outcomes, party comparisons, partly-filled columns, or a join
you have not written before. `database_query` carries only the column names; this carries
what they mean.

Takes no arguments.

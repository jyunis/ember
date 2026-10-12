"""Eval harness (learning loop Layer 2 — UoW-21 / FC-21).

Human-authored source. Loads golden-task cases from `/data/evals/cases/*.toml`,
runs them against the live config, scores them cheaply, and records results
keyed to a config fingerprint so a change to identity / skills / route can be
compared before/after.

Cases are the agent-forbidden write set (TLC-19 / doc #36 Decisions #6): this
package only ever READS them.
"""

"""Analysis primitives — hierarchy decomposition + methodology-driven prompts.

The goal of this package is to separate *what data the analyst sees* from
*what methodology the analyst follows*. Both are first-class concerns:

  - `campaign_decomposer.py` pulls the full parent→children tree for a
    given ad object (SP campaign, Meta campaign, …) and computes
    concentration ratios so the analyst doesn't have to.
  - Methodology prompts live in `playbooks/<client>.md` Section X and are
    injected into the LLM call by each analysis node.

Design premise: the dumb output we shipped before (e.g. "pause campaign
with 0.9× ROAS" when 80% of its spend was one winning keyword) was a
methodology failure, not a reasoning failure. Good structured data + a
disciplined prompt should fix it on any competent model.
"""

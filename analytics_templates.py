"""Offline website-model summarizer - 5 AI dictionary versions per type.
No Ollama, no network, no threads. Pure Python templates with slots.
Same return shape as old Ollama version so lgu_analytics.html is unchanged.
"""
import hashlib


def _variant_index(key: str, seed: str, n: int = 5) -> int:
    h = hashlib.md5(f"{key}|{seed}".encode("utf-8", errors="ignore")).hexdigest()
    return int(h, 16) % n


AI_VERSION_LABELS = ["AI v1 - Standard", "AI v2 - Executive", "AI v3 - Field Notes", "AI v4 - Action-Oriented", "AI v5 - Community Voice"]


def pick(key: str, seed: str):
    idx = _variant_index(key, seed or key)
    return idx, AI_VERSION_LABELS[idx], ANALYTICS_DICTIONARY[key][idx]


def render(key: str, seed: str, **slots):
    idx, label, tpl = pick(key, seed)
    class _Safe(dict):
        def __missing__(self, k):
            return ""
    try:
        text = tpl.format_map(_Safe(**{k: (v if v is not None else "") for k, v in slots.items()}))
    except Exception:
        text = tpl
    return idx, label, text


ANALYTICS_DICTIONARY = {
    "executive_summary": [
        "Barangay Picaleon's ActBayan system manages {total} citizen concerns with a {res_pct}% resolution rate. "
        "Top category is {top_cat} ({top_cat_count} reports) and most active location is {top_loc} ({top_loc_count}). "
        "Monthly average is {avg_monthly} over {months} months; latest month {latest_label} had {latest_count}. "
        "Recommendation: sustain operations in {top_loc} and fast-track {top_cat} cases toward {next_target}% resolution.",
        "Executive overview: {total} concerns logged, {resolved} resolved ({res_pct}%) and {pending} still pending. "
        "Hotspot {top_loc} has {top_loc_count} reports while {top_cat} leads categories with {top_cat_count}. "
        "Trend over {months} months averages {avg_monthly} monthly; recent month {latest_label} logged {latest_count}. "
        "Action: prioritize {top_cat} backlog and schedule a sweep in {top_loc}.",
        "ActBayan at a glance: {total} reports, {res_pct}% resolved ({resolved} of {total}). "
        "Citizens report {top_cat} most ({top_cat_count}) and {top_loc} tops locations ({top_loc_count}). "
        "In {latest_label}, {latest_count} arrived vs {avg_monthly} average. "
        "Next: assign teams to {top_loc} and push resolution toward {next_target}%.",
        "Decision makers: {total} filings, {resolved} resolved, {pending} actionable - {res_pct}% closure. "
        "{top_cat} ({top_cat_count}) and {top_loc} ({top_loc_count}) lead. "
        "{months}-month trend averages {avg_monthly}, peaking at {peak_count} in {peak_label}. "
        "Advice: replicate resolved-case workflow and aim for {next_target}%.",
        "Overall: {total} concerns, {res_pct}% resolved. Focus: '{top_cat}' ({top_cat_count}) and '{top_loc}' ({top_loc_count}). "
        "Latest {latest_label}: {latest_count} vs avg {avg_monthly}. {pending} need action. "
        "Plan: weekly {top_cat} review and coordination in {top_loc} toward {next_target}%.",
    ],
    "monthly_summary": [
        "Monthly trend across {months} months: {total_monthly} filings, avg {avg_monthly}. "
        "Busiest {peak_label} ({peak_count}); latest {latest_label} had {latest_count} with {latest_resolved} resolved. "
        "Capacity is stable. Monitor {latest_label} follow-ups to prevent backlog.",
        "Last {months} months: {total_monthly} concerns ({avg_monthly} avg). "
        "{peak_label} peaked at {peak_count}; {latest_label} added {latest_count}, resolved {latest_resolved}. "
        "Trend is {trend_word}. Keep teams staffed during peak weeks.",
        "Volume: {total_monthly} in {months} months (~{avg_monthly} monthly). High {peak_label} ({peak_count}); "
        "latest {latest_label} ({latest_count} new, {latest_resolved} resolved). Trend reads {trend_word}. "
        "Keep intake workflow and shorten verification time.",
        "Month review: {months} points totalling {total_monthly}. Avg {avg_monthly}, max {peak_count} in {peak_label}. "
        "Recent {latest_label}: {latest_count} new, {latest_resolved} resolved. Protect pace by clearing leftovers first.",
        "Snapshot: {total_monthly} over {months} months. Mean {avg_monthly}, peak {peak_count} ({peak_label}), "
        "latest {latest_count} ({latest_label}) with {latest_resolved} resolved. Pattern is {trend_word} demand. "
        "Deploy around busy months like {peak_label}.",
    ],
    "sample_summary": [
        "Spotlight: '{sample_title}' ({sample_category}, {sample_status}) from {sample_location}, filed {sample_when}. "
        "Excerpt: '{sample_excerpt}'. Mirrors the wider {sample_category} pattern. Prompt handling signals responsiveness in {sample_location}.",
        "Featured: '{sample_title}' - {sample_status} {sample_category} in {sample_location} ({sample_when}). "
        "Excerpt: '{sample_excerpt}'. Typical neighborhood filing with clear location and request. A fast win builds trust nearby.",
        "Focus case: '{sample_title}' [{sample_category}|{sample_status}] at {sample_location} ({sample_when}). "
        "'{sample_excerpt}'. Specific and verifiable on the map. Use as reference for the {sample_category} team.",
        "Sample review: '{sample_title}' from {sample_location} ({sample_category}, {sample_status}, {sample_when}). "
        "'{sample_excerpt}'. Clusters near {sample_location}, reinforcing hotspot analysis. Prioritize to cut repeats.",
        "Illustrative: '{sample_title}' - {sample_category} in {sample_location}, {sample_status} (filed {sample_when}). "
        "'{sample_excerpt}' Exemplifies good data quality: what, where, when. Closing the loop encourages more filings.",
    ],
    "status_summary": [
        "Status: {pending} pending ({pending_pct}%), {inprogress} in progress ({inprogress_pct}%), {resolved} resolved ({resolved_pct}%) of {total}. "
        "Pipeline is {pipeline_word}. Push in-progress items to closure and acknowledge new resolutions publicly.",
        "Workflow: pending {pending}, in-progress {inprogress}, resolved {resolved} (total {total}). Share resolved {resolved_pct}%. "
        "Queue looks {pipeline_word}; convert the {pending} pending into assignments this week.",
        "Mix: {resolved} resolved ({resolved_pct}%), {inprogress} ongoing ({inprogress_pct}%), {pending} awaiting ({pending_pct}%) of {total}. "
        "Throughput is {pipeline_word}. Daily check-ins until resolved share passes {next_target}%.",
        "Queue: {pending} new, {inprogress} handled, {resolved} done of {total}. {resolved_pct}% closed. "
        "Momentum is {pipeline_word} - clear oldest pending first and document each resolution.",
        "Health: resolved {resolved}/{total} ({resolved_pct}%), active {inprogress}, queued {pending}. "
        "{pipeline_word} operations. Triage {pending} pending by urgency and publish notes for transparency.",
    ],
    "location_summary": [
        "Spread: {n_locations} locations. {top_loc} leads ({top_loc_count}), then {second_loc} ({second_count}), {third_loc} ({third_count}). "
        "Clustering near {top_loc} suggests targeted action. Repeat visits to {top_loc} should cut recurrence.",
        "Analysis over {n_locations} zones: {top_loc} ({top_loc_count}), {second_loc} ({second_count}), {third_loc} ({third_count}). "
        "{top_loc} needs a zone plan with purok leaders. Reports are verifiable on the map.",
        "Where: {top_loc} tops ({top_loc_count} of {total}), {second_loc} ({second_count}), {third_loc} ({third_count}). "
        "{n_locations} locations. Inspect {top_loc} first, then sweep {second_loc}.",
        "Hotspots ({n_locations}): {top_loc} {top_loc_count}; {second_loc} {second_count}; {third_loc} {third_count}. "
        "{top_loc} needs sustained attention. Weekly monitoring round recommended.",
        "Spatial: {n_locations} locations, {top_loc} highest ({top_loc_count}). Runners: {second_loc} ({second_count}), {third_loc} ({third_count}). "
        "Focus on {top_loc} while watching {second_loc} to prevent spillover.",
    ],
    "category_summary": [
        "Mix across {n_cats} types: {top_cat} ({top_cat_count}), {second_cat} ({second_count_c}), {third_cat} ({third_count_c}). "
        "{top_cat} dominance points to systemic needs. A standing {top_cat} task force would cut resolution time.",
        "By type ({n_cats}): {top_cat} {top_cat_count}, {second_cat} {second_count_c}, {third_cat} {third_count_c} of {total}. "
        "{top_cat} is priority. Standardize its playbook: checklist, SLA, photo verification.",
        "Most reported: {top_cat} ({top_cat_count}), {second_cat} ({second_count_c}), {third_cat} ({third_count_c}). "
        "{n_cats} tracked. Skew to {top_cat} suggests preventive work beats reactive fixes.",
        "Thematic: {top_cat} largest ({top_cat_count} of {total}), {second_cat} ({second_count_c}), {third_cat} ({third_count_c}). "
        "Treat {top_cat} as flagship: budget, weekly review, public progress posts.",
        "Health ({n_cats}): {top_cat} {top_cat_count}; {second_cat} {second_count_c}; {third_cat} {third_count_c}. "
        "Since {top_cat} recurs, root-cause work matters most. Track repeat-rate as metric.",
    ],
}

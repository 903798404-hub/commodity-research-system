"""Collect only implemented official sources into a fresh external four-board store."""
from __future__ import annotations

from datetime import date, timedelta
import re

from agri_research_agent.automation import production_data_delta as delivery
from agri_research_agent.data_sources.nutstore_basis import assert_external_output
from agri_research_agent.oilseed_positions.model import config as domain_config
from agri_research_agent.oilseed_positions.sources import Sources
from agri_research_agent.positions import delivery as codec
from agri_research_agent.positions.bundle import verified_files
from agri_research_agent.sugar_positions.sources import OfficialSources
from agri_research_agent.sugar_positions.storage import publish, read_snapshot


def collect_bundle(baseline, work, *, markets, today=None, cftc_annual=False, stock_source=None, incremental=False):
    today = today or date.today()
    delivery.require(markets in {"domestic", "foreign"}, "scheduled positions market scope invalid")
    builder = codec.IncrementBuilder(baseline, delivery.ROOT) if incremental else None
    snapshots = builder.snapshots if builder else codec.validate_archive(baseline, delivery.ROOT)[0]
    store = assert_external_output(work / "collection")
    if not incremental:
        codec.materialize(baseline, store)
    publish_rows = builder.publish if builder else publish
    sugar = delivery._module(delivery.ROOT, "04_scripts/sugar_positions/update_sugar_positions.py", "scheduled_sugar")
    oilseed = delivery._module(delivery.ROOT, "04_scripts/oilseed_positions/update_positions.py", "scheduled_oilseed")
    specs, attempts = domain_config(delivery.ROOT), []
    annual=None
    if cftc_annual:
        delivery.require(markets=='foreign','annual CFTC collection is foreign only')
        from agri_research_agent.positions.cftc_annual import AnnualSources
        annual=AnnualSources(today)
    selected = ("sugar", "rapeseed") if markets == "domestic" else ("sugar", "rapeseed", "soybean")
    if stock_source is not None:
        delivery.require(markets == 'domestic', 'stock API is domestic only')
        selected = (*selected, 'soybean', 'palm')
    windows = {}
    for domain in selected:
        if domain in ('soybean', 'palm') and stock_source is not None:
            continue  # One complete DCE response is shared by these two boards.
        old = snapshots[domain]
        # Keep CFTC query identity and all already published years covered by that source.
        years = [int(match.group(1)) for source in old["sources"].values()
                 if (match := re.search(r"(20\d{2})-01-01T00", source["url"]))]
        start_year = min(years, default=today.year - 1)
        if incremental and cftc_annual:
            # Annual providers are indivisible. Include the preceding year in
            # the 35-day rollover window so late December reports are not lost.
            start_year = (today - timedelta(days=35)).year
        existing = {row["report_date"] for row in old["domestic"]}
        start_day, end_day = domestic_window(existing, today) if markets == "domestic" else (today, today)
        windows[domain] = {"start": start_day.isoformat(), "end": end_day.isoformat(),
                           "target": today.isoformat(), "catchup_remaining": end_day < today}
        if domain == "sugar":
            rows = sugar.collect(OfficialSources(), markets=markets, start_year=start_year,
                start_day=start_day, end_day=end_day, existing_dates=existing,include_cftc=not cftc_annual)
        else:
            # No DCE, browser, Sina or pending FCPO requests in this automatic chain.
            spec = specs[domain]
            rows = oilseed.collect(Sources(), spec, markets=markets, start_year=start_year,
                start_day=start_day, end_day=end_day, euro_start=max(date(2026, 9, 30), today - timedelta(days=35)),
                existing_dates=existing,include_cftc=not cftc_annual)
        foreign, domestic, captures, observed = rows
        if annual:
            more,raws,checks=annual.collect(domain,specs.get(domain),old['foreign'],start_year)
            foreign.extend(more);captures.extend(raws);observed.extend(checks)
        attempts.extend({"domain": domain, **item} for item in observed)
        root = store / domain
        if not incremental:
            (root / "last_attempt.json").write_bytes(delivery.canonical_json_bytes({"attempts": observed}))
        if foreign or domestic:
            publish_rows(root, foreign, domestic, captures, observed)
    if stock_source is not None:
        observed, window = collect_dce(stock_source, snapshots, store, today, publish_rows=publish_rows, incremental=incremental)
        attempts.extend(observed)
        windows['dce'] = window
    bundle = assert_external_output(work / "bundle")
    (work / "collection-window.json").write_bytes(delivery.canonical_json_bytes(windows))
    if incremental:
        document, semantic = builder.finish(delivery.ROOT)
        (work / "increment-observations.json").write_bytes(delivery.canonical_json_bytes(semantic))
        return document, attempts
    bundle.mkdir()
    manifest = {"schema_version": "commodity-positions-bundle/1", "domains": {}, "files": {}}
    for domain in codec.DOMAINS:
        snapshot, files = verified_files(store / domain, delivery.ROOT, domain)
        manifest["domains"][domain] = {"release_id": snapshot["release_id"],
            "foreign_rows": len(snapshot["foreign"]), "domestic_rows": len(snapshot["domestic"])}
        for rel, raw in files.items():
            target = bundle / "domains" / domain / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            manifest["files"][target.relative_to(bundle).as_posix()] = codec.digest(raw)
    (bundle / "bundle.json").write_bytes(codec.canonical(manifest))
    candidate = codec.archive_bundle(bundle, delivery.ROOT)
    semantic = codec.observations(candidate, baseline, delivery.ROOT)
    # Normal scheduled increments never silently approve changes to published report partitions.
    delivery.require(not semantic["revised_partitions"], "positions historical revision requires review")
    # Boards and markets outside the request must retain exact verified inputs.
    current = codec.validate_archive(candidate, delivery.ROOT)[0]
    for domain in codec.DOMAINS:
        if domain not in selected:
            delivery.require(current[domain] == snapshots[domain], "unselected positions board changed")
        else:
            untouched = "foreign" if markets == "domestic" else "domestic"
            delivery.require(current[domain][untouched] == snapshots[domain][untouched],
                             "unselected positions market changed")
    return bundle, attempts


def collect_dce(source, snapshots, store, today, *, publish_rows=publish, incremental=False):
    """Append verified all-contract disclosures; preserve same-day/manual partitions."""
    from agri_research_agent.oilseed_positions.stock_api import StockApiError
    from agri_research_agent.oilseed_positions.sources import SourceNotPublished
    latest=[]
    for domain,varieties in (('soybean',('M','Y')),('palm',('P',))):
        for variety in varieties:
            dates={r['report_date'] for r in snapshots[domain]['domestic']
                   if (r['scope']==variety if incremental else r['scope'].rstrip('0123456789')==variety)}
            if dates: latest.append(max(dates))
    start,end=domestic_window({min(latest)} if len(latest)==3 else set(),today)
    attempts=[];by_domain={'soybean':[],'palm':[]}
    cursor=start
    while cursor<=end:
        if cursor.weekday()<5:
            source_id=f'stockapi_DCE_{cursor:%Y%m%d}'
            covered = all(any(r['report_date'] == cursor.isoformat() and r['scope'] == variety
                             for r in snapshots[domain]['domestic'])
                          for domain, varieties in (('soybean', ('M', 'Y')), ('palm', ('P',)))
                          for variety in varieties)
            if incremental and covered:
                for domain in by_domain:
                    observed = dict(domain=domain, source_id=source_id, status='no_change', rows=0,
                                    reason='published_variety_summaries_present')
                    attempts.append(observed); by_domain[domain].append(observed)
                cursor += timedelta(days=1)
                continue
            try:
                rows,raw,url=source.holdings(cursor,('M','Y','P'))
                delivery.require(rows and all(r['report_date']==cursor.isoformat() for r in rows),
                                 'DCE source date differs')
                for domain,varieties in (('soybean',{'M','Y'}),('palm',{'P'})):
                    previous=snapshots[domain]['domestic']
                    existing={(r['scope'],r['report_date']) for r in previous}
                    current=[r for r in rows if r['scope'].rstrip('0123456789') in varieties
                             and (r['scope'],r['report_date']) not in existing]
                    observed=dict(domain=domain,source_id=source_id,status='ok' if current else 'no_change',rows=len(current))
                    if current:publish_rows(store/domain,[],current,[(source_id,raw,url,'json')],[observed],preserve_untouched=True)
                    attempts.append(observed);by_domain[domain].append(observed)
            except (StockApiError,SourceNotPublished,ValueError) as exc:
                # Never persist arbitrary upstream exception text or authentication messages.
                status='not_published' if isinstance(exc,SourceNotPublished) else 'failed'
                for domain in by_domain:
                    observed=dict(domain=domain,source_id=source_id,status=status,error='stock_api_report_not_published' if status=='not_published' else 'stock_api_source_unavailable_or_invalid')
                    attempts.append(observed);by_domain[domain].append(observed)
        cursor+=timedelta(days=1)
    if not incremental:
        for domain,observed in by_domain.items():
            (store/domain/'last_attempt.json').write_bytes(delivery.canonical_json_bytes({'attempts':observed}))
    return attempts,dict(start=start.isoformat(),end=end.isoformat(),target=today.isoformat(),catchup_remaining=end<today)


def domestic_window(existing_dates, today):
    """Resume after actual published observations in bounded 31-day batches."""
    if not existing_dates:
        return today, today
    latest = max(date.fromisoformat(day) for day in existing_dates)
    delivery.require(latest <= today, "positions baseline is future dated")
    start = min(today, latest + timedelta(days=1))
    return start, min(today, start + timedelta(days=30))

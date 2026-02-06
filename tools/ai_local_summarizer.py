# -*- coding: utf-8 -*-
"""
Free/local AOI report generator for ArchToolkit.

This module intentionally makes no external API calls.
It turns the AOI context (from ai_aoi_summary.build_aoi_context) into a
Korean, report-like narrative.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def _fmt_int(v: Any) -> str:
    try:
        if v is None:
            return "-"
        return f"{int(v):,}"
    except Exception:
        return str(v)


def _fmt_float(v: Any, *, digits: int = 2) -> str:
    try:
        if v is None:
            return "-"
        x = float(v)
        if not math.isfinite(x):
            return "-"
        return f"{x:,.{int(digits)}f}"
    except Exception:
        return str(v)


def _first_nonempty(items: List[str]) -> Optional[str]:
    for s in items:
        if isinstance(s, str) and s.strip():
            return s.strip()
    return None


def _layer_stats_lines(layer: Dict[str, Any]) -> List[str]:
    t = str(layer.get("type") or "")
    stats = layer.get("stats")
    if not stats:
        return ["- 통계: (없음/계산 실패)"]

    lines: List[str] = []

    if t == "vector":
        n = stats.get("features")
        scanned = stats.get("scanned")
        lines.append(f"- 피처: { _fmt_int(n) } (스캔 { _fmt_int(scanned) })")

        if "total_length_m" in stats:
            lines.append(f"- 총 길이: { _fmt_float(stats.get('total_length_m'), digits=1) } m")
        if "total_area_m2" in stats:
            lines.append(f"- 총 면적: { _fmt_float(stats.get('total_area_m2'), digits=1) } ㎡")

        top_field = stats.get("top_field")
        top_vals = stats.get("top_values") or []
        if top_field and top_vals:
            preview = ", ".join([f"{d.get('value')}={_fmt_int(d.get('count'))}" for d in top_vals[:6]])
            if preview:
                lines.append(f"- 상위 값({top_field}): {preview}")

        num = stats.get("numeric_fields") or {}
        if isinstance(num, dict) and num:
            preferred = [
                # Viewshed AOI stats
                "vis_pct",
                "vis_m2",
                "tot_m2",
                # Cost/LCP
                "dist_m",
                "time_min",
                "energy_kcal",
                # Terrain profile
                "distance",
                "min_elev",
                "max_elev",
                # GeoChem
                "val_min",
                "val_max",
                "v_min",
                "v_max",
                # Cadastral
                "in_aoi_pct",
                "in_aoi_m2",
                "parcel_m2",
            ]
            show_fields = [f for f in preferred if f in num]
            if not show_fields:
                show_fields = list(num.keys())
            for f in show_fields[:8]:
                d = num.get(f) or {}
                lines.append(
                    f"- {f}: mean={_fmt_float(d.get('mean'), digits=2)} (min={_fmt_float(d.get('min'), digits=2)}, max={_fmt_float(d.get('max'), digits=2)}, n={_fmt_int(d.get('n'))})"
                )

        dist = stats.get("dist_to_aoi_centroid_m")
        if isinstance(dist, dict) and dist.get("n"):
            lines.append(
                f"- AOI 중심까지 거리: mean={_fmt_float(dist.get('mean'), digits=1)} m (min={_fmt_float(dist.get('min'), digits=1)}, max={_fmt_float(dist.get('max'), digits=1)}, n={_fmt_int(dist.get('n'))})"
            )

    elif t == "raster":
        lines.append(f"- 픽셀(표본) 수: {_fmt_int(stats.get('count'))}")
        lines.append(
            f"- min/mean/max: { _fmt_float(stats.get('min'), digits=3) } / { _fmt_float(stats.get('mean'), digits=3) } / { _fmt_float(stats.get('max'), digits=3) }"
        )
        if "gt_0_5_pct" in stats:
            lines.append(f"- (힌트) 마스크/가시(>0.5) 비율: { _fmt_float(stats.get('gt_0_5_pct'), digits=1) } %")
    else:
        lines.append("- 통계: (지원되지 않는 레이어 타입)")

    return lines


def generate_report(ctx: Dict[str, Any]) -> str:
    aoi = ctx.get("aoi") or {}
    layers = ctx.get("layers") or []
    options = ctx.get("options") or {}

    aoi_name = str(aoi.get("layer_name") or "").strip()
    aoi_crs = str(aoi.get("crs") or "").strip()
    feat_n = aoi.get("feature_count")
    aoi_area = aoi.get("area_m2")

    radius_m = ctx.get("radius_m")
    buf_area = ctx.get("buffer_area_m2")

    selected_only = bool(options.get("selected_only")) if "selected_only" in options else None
    arch_only = bool(options.get("archtoolkit_only")) if "archtoolkit_only" in options else None

    mode_note = _first_nonempty(
        [
            "ArchToolkit 결과 중심 요약" if arch_only else None,
            "프로젝트 전체 요약" if arch_only is False else None,
        ]
    )
    sel_note = _first_nonempty(
        [
            "선택 피처만 사용" if selected_only else None,
            "레이어 전체 피처 사용" if selected_only is False else None,
        ]
    )

    header_notes = " / ".join([s for s in [mode_note, sel_note] if s])
    if header_notes:
        header_notes = f"({header_notes})"

    out: List[str] = []
    out.append("# AI 조사요약 (무료/로컬)")
    out.append("")

    # 1) Overview
    out.append("## 1) 개요")
    out.append(f"- AOI: `{aoi_name or '(이름 없음)'}`")
    out.append(f"- AOI 피처 수: {_fmt_int(feat_n)}")
    out.append(f"- AOI CRS: `{aoi_crs or '-'}`")
    out.append(f"- AOI 면적: {_fmt_float(aoi_area, digits=1)} ㎡")
    out.append(f"- 반경: {_fmt_float(radius_m, digits=0)} m")
    out.append(f"- 버퍼 면적(반경 내): {_fmt_float(buf_area, digits=1)} ㎡")
    out.append(f"- 요약 레이어 수: {_fmt_int(len(layers))} {header_notes}".rstrip())
    if isinstance(options.get("max_layers"), (int, float)) and len(layers) >= int(options.get("max_layers") or 0):
        out.append("- 참고: 레이어 수가 많아 일부만 요약되었을 수 있습니다.")
    out.append("")

    # 2) Layer summaries
    out.append("## 2) 레이어/분석 요약")
    if not layers:
        out.append("- (요약할 레이어가 없습니다)")
    else:
        for lyr in layers:
            name = str(lyr.get("name") or "").strip() or "(이름 없음)"
            ltype = str(lyr.get("type") or "")
            group_path = str(lyr.get("group_path") or "").strip()
            wkb = str(lyr.get("wkb") or "").strip()
            arch = lyr.get("archtoolkit") or {}
            tool_id = str((arch.get("tool_id") if isinstance(arch, dict) else "") or "").strip()
            run_id = str((arch.get("run_id") if isinstance(arch, dict) else "") or "").strip()
            kind = str((arch.get("kind") if isinstance(arch, dict) else "") or "").strip()
            units = str((arch.get("units") if isinstance(arch, dict) else "") or "").strip()
            created_at = str((arch.get("created_at") if isinstance(arch, dict) else "") or "").strip()

            meta_bits = []
            if ltype:
                meta_bits.append(ltype)
            if wkb:
                meta_bits.append(wkb)
            if group_path:
                meta_bits.append(group_path)
            meta = " | ".join(meta_bits)
            out.append(f"### - {name}")
            if meta:
                out.append(f"- 분류: {meta}")
            if tool_id or run_id:
                meta2 = []
                if tool_id:
                    meta2.append(f"tool_id={tool_id}")
                if kind:
                    meta2.append(f"kind={kind}")
                if units:
                    meta2.append(f"units={units}")
                if run_id:
                    meta2.append(f"run_id={run_id}")
                if created_at:
                    meta2.append(f"created_at={created_at}")
                out.append(f"- ArchToolkit 메타: {', '.join(meta2)}")
            out.extend(_layer_stats_lines(lyr))
            out.append("")

    # 3) Key observations (heuristic)
    out.append("## 3) 핵심 관찰(로컬 자동 요약)")
    observations: List[str] = []
    try:
        # Find the biggest vector layer by feature count
        vec = []
        for lyr in layers:
            if str(lyr.get("type") or "") != "vector":
                continue
            stats = lyr.get("stats") or {}
            vec.append((int(stats.get("features") or 0), str(lyr.get("name") or "")))
        vec.sort(reverse=True)
        if vec and vec[0][0] > 0:
            observations.append(f"- 주변에서 가장 많은 피처가 겹치는 레이어: `{vec[0][1]}` ({_fmt_int(vec[0][0])}개)")
    except Exception:
        pass

    try:
        ras_vis = []
        for lyr in layers:
            if str(lyr.get("type") or "") != "raster":
                continue
            stats = lyr.get("stats") or {}
            if "gt_0_5_pct" in stats:
                ras_vis.append((float(stats.get("gt_0_5_pct") or 0.0), str(lyr.get("name") or "")))
        ras_vis.sort(reverse=True)
        if ras_vis:
            observations.append(
                f"- (힌트) 0.5 초과 비율이 높은 래스터: `{ras_vis[0][1]}` ({_fmt_float(ras_vis[0][0], digits=1)}%)"
            )
    except Exception:
        pass

    if not observations:
        observations.append("- (특이사항 자동 추출 없음) 위 레이어별 통계를 참고해 해석하세요.")

    out.extend(observations)
    out.append("")

    # 4) Limits
    out.append("## 4) 한계/주의")
    out.append("- 이 보고서는 **외부 AI를 호출하지 않는 로컬 요약**입니다(문장 품질/해석은 제한적).")
    out.append("- 통계는 AOI 버퍼와의 교차/표본 기반이며, 레이어 품질(좌표계/해상도/NoData)에 따라 달라질 수 있습니다.")
    out.append("- 레이어가 많거나(레이어 cap), 피처가 매우 많으면(스캔 cap) 일부만 반영되었을 수 있습니다.")
    out.append("")

    # 5) Next steps
    out.append("## 5) 다음 단계 제안")
    out.append("- 필요하면 `AI 모드: Gemini(API)`로 전환해 더 자연어 중심의 보고서 문장을 생성합니다.")
    out.append("- 해석에 중요한 레이어는 이름/그룹을 정리하고(민감정보 제거) 다시 요약을 생성합니다.")
    out.append("- 최종 보고서에는 원자료/방법/좌표계/해상도 등을 함께 기록하세요.")
    out.append("")

    return "\n".join(out).strip() + "\n"

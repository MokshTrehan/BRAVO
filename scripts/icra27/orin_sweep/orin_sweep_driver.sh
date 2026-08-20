#!/usr/bin/env bash
# ORIN-SWEEP-1 host driver (Orin HOST, not the container). Queue-driven, serialized, append-only.
#
#   orin_sweep_driver.sh <root>
#
# Reads <root>/QUEUE.tsv line by line (cursor in <root>/state/cursor). Each line:
#   name<TAB>dataset<TAB>budget<TAB>mode<TAB>repeat<TAB>bag<TAB>bag_start<TAB>launch
# budget "C5" + mode "C5" = gate replay with the C5 launch and no overrides.
# A line "STOP" ends the driver once reached. An empty queue tail makes the driver wait.
# Per cell: swap check, thermal gate, tegrastats + thermal sampler capture, container replay through
# the byte-identical C5 cell runner, 13 s power tail, verification, SHA256SUMS, RUN_LOG/runs.jsonl append.
# Any INVALID_THERMAL verdict enqueues exactly one repeat (suffix "t") after a 300 s cool-down.
set -Eeuo pipefail
root=$1
tool=/data/tooling/sweep
queue="${root}/QUEUE.tsv"; state="${root}/state"; power="${root}/power"; cells="${root}/cells"
mkdir -p "${state}" "${power}" "${cells}"
cursor_file="${state}/cursor"; [[ -f "${cursor_file}" ]] || echo 0 > "${cursor_file}"
runlog="${root}/RUN_LOG.md"; runs="${root}/runs.jsonl"
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "${root}/driver.log"; }
runlog() { echo "- $(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "${runlog}"; }
cpu_temp() { cat /sys/class/thermal/thermal_zone0/temp; }
stop_power() {
    sudo pkill -KILL -x tegrastats 2>/dev/null || true
    [[ -n "${sampler_pid:-}" ]] && { kill "${sampler_pid}" 2>/dev/null || true; }
    sampler_pid=""
}
trap 'stop_power' EXIT
echo "# ORIN-SWEEP-1 RUN_LOG (board copy, append-only) — root ${root}" >> "${runlog}"
log "driver start root=${root} pid=$$"

# standing proofs at driver start
swapon --show > "${state}/driver_start_swapon.txt" || true
if [[ -s "${state}/driver_start_swapon.txt" ]]; then log "REFUSED: swap active"; exit 2; fi
sudo nvpmodel -q > "${state}/driver_start_nvpmodel.txt"
sha256sum /data/schurvio-lite-frozen/build/cp0-ws/devel/lib/ov_msckf/ros1_serial_msckf > "${state}/driver_start_binary.sha256"
( cd /data/schurvio-lite-frozen && git rev-parse HEAD && git status --porcelain | wc -l ) > "${state}/driver_start_tree.txt"

while true; do
    cur=$(cat "${cursor_file}")
    line=$(sed -n "$((cur + 1))p" "${queue}" || true)
    if [[ -z "${line}" ]]; then sleep 20; continue; fi
    if [[ "${line}" == "STOP" ]]; then log "STOP reached at cursor ${cur}"; break; fi
    IFS=$'\t' read -r name dataset budget mode repeat bag bag_start launch <<< "${line}"
    cell="${cells}/${name}"
    if [[ -d "${cell}" ]]; then log "SKIP existing ${name}"; echo $((cur + 1)) > "${cursor_file}"; continue; fi
    mkdir -p "${cell}/diagnostics"
    # swap proof per cell
    if [[ -n "$(swapon --show)" ]]; then log "REFUSED swap active before ${name}"; runlog "${name} REFUSED_SWAP"; exit 2; fi
    # thermal gate: start each cell from a comparable thermal state (<= 50 C, wait up to 600 s)
    waited=0
    while (( $(cpu_temp) > 50000 && waited < 600 )); do sleep 5; waited=$((waited + 5)); done
    pre_t=$(cpu_temp)
    if [[ "${budget}" == "C5" ]]; then extra=(); else extra=("num_pts:=${budget}" "landmark_elimination:=$([[ ${mode} == S1 ]] && echo schur || echo nullspace)"); fi
    # D9: warm the page cache for the bag identically before every cell (duration recorded)
    bw0=$(date +%s.%N); cat "${bag}" > /dev/null; bag_warm_s=$(python3 -c "print(round($(date +%s.%N)-${bw0},1))")
    log "START ${name} pre_cpu_mC=${pre_t} waited=${waited}s bag_warm_s=${bag_warm_s} extra=[${extra[*]:-}]"
    sudo tegrastats --interval 1000 > "${power}/${name}_tegrastats.txt" &
    bash "${tool}/orin_thermal_sampler.sh" "${power}/${name}_cooling.txt" &
    sampler_pid=$!
    sleep 2
    t_start=$(date +%s.%N)
    status=0
    sudo docker run --rm --name "sweep-${name}" -u 1000:1000 \
        -v /data/schurvio-lite-frozen:/repo -v /data:/data schurvio-orin-env:c5-run \
        bash "${tool}/orin_cell_run.sh" "${cell}" 11411 1-5 "${launch}" "${bag}" "${bag_start}" "${dataset}" "${extra[@]}" \
        > "${cell}/diagnostics/container_stdout.txt" 2>&1 || status=$?
    t_end=$(date +%s.%N)
    sleep 13   # power tail: the C5 convention slices [capture start, start + wall + 12 s]
    stop_power
    { date -u +%Y-%m-%dT%H:%M:%SZ; for z in /sys/class/thermal/thermal_zone*; do
        printf '%s %s\n' "$(cat "$z/type")" "$(cat "$z/temp" 2>/dev/null || echo NA)"; done
    } > "${power}/${name}_post_thermal.txt"
    sudo chown -R 1000:1000 "${cell}" 2>/dev/null || true
    vline=$(python3 "${tool}/orin_sweep_verify_cell.py" --cell "${cell}" --name "${name}" --dataset "${dataset}" \
        --budget "${budget}" --mode "${mode}" --launch "${launch}" --cooling "${power}/${name}_cooling.txt" \
        --t-start "${t_start}" --t-end "${t_end}" 2>&1 | tail -1)
    printf 'name=%s\ndataset=%s\nbudget=%s\nmode=%s\nrepeat=%s\nbag=%s\nbag_start=%s\nlaunch=%s\ncontainer_exit=%s\nt_start=%s\nt_end=%s\npre_cpu_mC=%s\nthermal_gate_wait_s=%s\nbag_warm_s=%s\n' \
        "${name}" "${dataset}" "${budget}" "${mode}" "${repeat}" "${bag}" "${bag_start}" "${launch}" "${status}" "${t_start}" "${t_end}" "${pre_t}" "${waited}" "${bag_warm_s}" \
        > "${cell}/diagnostics/host_invocation.txt"
    cp "${power}/${name}_tegrastats.txt" "${power}/${name}_cooling.txt" "${power}/${name}_post_thermal.txt" "${cell}/diagnostics/" 2>/dev/null || true
    ( cd "${cell}" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
    verdict=$(python3 -c "import json;print(json.load(open('${cell}/diagnostics/thermal.json'))['verdict'])" 2>/dev/null || echo UNKNOWN)
    log "DONE ${name} container_exit=${status} ${vline}"
    runlog "${name} budget=${budget} mode=${mode} r=${repeat} container_exit=${status} :: ${vline}"
    python3 - "${cell}" "${name}" "${dataset}" "${budget}" "${mode}" "${repeat}" "${status}" "${runs}" <<'PY'
import json, sys
cell, name, ds, b, m, r, st, out = sys.argv[1:]
d = json.load(open(cell + "/diagnostics/cell_delta.json")); s = json.load(open(cell + "/diagnostics/cell_summary.json")); t = json.load(open(cell + "/diagnostics/thermal.json"))
rec = {"name": name, "dataset": ds, "budget": b, "mode": m, "repeat": r, "container_exit": int(st), "status": s["status"],
       "delta_verified": d["delta_verified"], "effective_num_pts": d["effective_num_pts"], "elimination": d["resolved_elimination_ros"],
       "thermal": t["verdict"], "n": s["timing"].get("n_callbacks"), "p50": s["timing"].get("total_ms", {}).get("p50"),
       "p99": s["timing"].get("total_ms", {}).get("p99"), "compliance_pct": s["timing"].get("deadline_compliance_pct"),
       "state_sha": s["state_estimate_sha256"], "wall_s": s["wall_elapsed_s"], "rss_kb": s["peak_rss_kb"]}
open(out, "a").write(json.dumps(rec) + "\n")
PY
    echo $((cur + 1)) > "${cursor_file}"
    if [[ "${verdict}" == "INVALID_THERMAL" && "${name}" != *t ]]; then
        log "INVALID_THERMAL ${name}: cool-down 300 s, enqueue one repeat ${name}t"
        runlog "${name} INVALID_THERMAL -> cool-down 300 s -> one repeat ${name}t enqueued (prereg rule)"
        printf '%st\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${name}" "${dataset}" "${budget}" "${mode}" "${repeat}" "${bag}" "${bag_start}" "${launch}" >> "${queue}.thermal"
        # insert the repeat right after the current line (queue stays append-only in spirit: we keep an audit copy)
        cp "${queue}" "${state}/QUEUE.before_thermal_insert.$(date -u +%s).tsv"
        { head -n "$((cur + 1))" "${queue}"; tail -n 1 "${queue}.thermal"; tail -n "+$((cur + 2))" "${queue}"; } > "${queue}.new" && mv "${queue}.new" "${queue}"
        sleep 300
    fi
    # cool-down between cells: back to <= 46 C or 90 s, whichever first
    cd_wait=0
    while (( $(cpu_temp) > 46000 && cd_wait < 90 )); do sleep 3; cd_wait=$((cd_wait + 3)); done
done
swapon --show > "${state}/driver_end_swapon.txt" || true
sha256sum /data/schurvio-lite-frozen/build/cp0-ws/devel/lib/ov_msckf/ros1_serial_msckf > "${state}/driver_end_binary.sha256"
( cd /data/schurvio-lite-frozen && git rev-parse HEAD && git status --porcelain | wc -l ) > "${state}/driver_end_tree.txt"
log "DRIVER_COMPLETE"

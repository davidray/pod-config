"""ComputeProvider backed by Runpod Pods (REST v2)."""

from __future__ import annotations

import hashlib
import json
import secrets as pysecrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from qwenbench.config import Config, Profile, format_duration
from qwenbench.paths import state_dir
from qwenbench.providers.base import ComputeStatus, Endpoint
from qwenbench.providers.openai_compat import OpenAICompatibleModel
from qwenbench.readiness import LABELS, Observation, Phase, ReadinessMachine
from qwenbench.runpod import podspec
from qwenbench.runpod.client import RunpodClient, RunpodError
from qwenbench.runpod.models import NetworkVolume, Pod
from qwenbench.secrets import get_secret, redact, require_secret
from qwenbench.state import Session, clear_session, load_session, log_event, save_session

Progress = Callable[[str], None]


class GuardViolation(RuntimeError):
    """A spend guard refused the operation."""


class ProvisionError(RuntimeError):
    pass


@dataclass
class UpOptions:
    idle_timeout_s: float | None | str = "default"   # "default" = profile value; None = disabled
    max_session_s: float | None | str = "default"
    max_spend_usd: float | None | str = "default"
    startup_timeout_s: float | None = None
    allow_concurrent: bool = False
    keep_on_failure: bool = False
    poll_s: float = 10.0


def config_fingerprint(profile: Profile) -> str:
    blob = json.dumps({"argv": podspec.vllm_argv(profile), "image": profile.model.image,
                       "gpu": profile.gpu_type_id, "cloud": profile.cloud}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def apply_options(profile: Profile, opts: UpOptions) -> Profile:
    updates: dict[str, Any] = {}
    if opts.idle_timeout_s != "default":
        updates["idle_timeout_s"] = opts.idle_timeout_s
    if opts.max_session_s != "default":
        updates["max_session_s"] = opts.max_session_s
    if opts.max_spend_usd != "default":
        updates["max_spend_usd"] = opts.max_spend_usd
    if opts.startup_timeout_s:
        updates["startup_timeout_s"] = opts.startup_timeout_s
    return profile.model_copy(update=updates)


class RunpodPodsProvider:
    name = "runpod-pods"

    def __init__(self, cfg: Config, client: RunpodClient | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self._client = client
        self._transport = transport  # for endpoint/watchdog HTTP in tests
        self.sleep = sleep
        self.clock = clock

    @property
    def client(self) -> RunpodClient:
        if self._client is None:
            api = self.cfg.runpod.api
            self._client = RunpodClient(require_secret("RUNPOD_API_KEY"), api.base_url, api.request_timeout_s)
        return self._client

    # ------------------------------------------------------------------ discovery

    def our_pods(self) -> list[Pod]:
        prefix = self.cfg.runpod.api.pod_name_prefix
        return [p for p in self.client.list_pods() if p.name.startswith(prefix) and p.status != "TERMINATED"]

    def pods_for(self, profile: Profile) -> list[Pod]:
        return [p for p in self.our_pods() if p.name == profile.pod_name]

    def find_volume(self, profile: Profile) -> NetworkVolume | None:
        name = profile.storage.volume_name
        matches = [v for v in self.client.list_network_volumes() if v.name == name]
        if len(matches) > 1:
            raise ProvisionError(f"multiple network volumes named {name!r}; delete the extras in the Runpod console")
        return matches[0] if matches else None

    def available_data_centers(self, profile: Profile) -> list[str]:
        """Profile candidates ordered by live availability (unknown last-but-kept)."""
        rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, None: 3, "NONE": 9}
        levels: dict[str, str | None] = {dc: None for dc in profile.data_center_ids}
        try:
            for dc in self.client.datacenters():
                if dc.get("id") not in levels:
                    continue
                for g in dc.get("gpuAvailability") or []:
                    if g.get("id") == profile.gpu_type_id:
                        levels[dc["id"]] = g.get("availability")
        except RunpodError:
            pass
        ordered = sorted(profile.data_center_ids, key=lambda d: rank.get(levels[d], 3))
        return [d for d in ordered if levels[d] != "NONE"] or ordered

    def ensure_volume(self, profile: Profile, progress: Progress) -> NetworkVolume | None:
        if profile.storage.mode != "network-volume":
            return None
        vol = self.find_volume(profile)
        if vol:
            if profile.data_center_ids and vol.data_center not in profile.data_center_ids:
                progress(f"warning: volume {vol.name} is in {vol.data_center}, not in the profile's "
                         f"data_center_ids {profile.data_center_ids}; using it anyway")
            return vol
        # Creating a volume starts a monthly charge; don't do it for a GPU with no stock.
        catalog = self.client.gpu_catalog(profile.gpu_type_id, cloud=profile.cloud)
        if catalog and catalog[0].get("availability") == "NONE":
            raise ProvisionError(f"{profile.gpu_type_id} ({profile.cloud}) has no availability right now; "
                                 "not creating its cache volume. Retry later (`qwenbench doctor` shows stock).")
        dcs = self.available_data_centers(profile)
        if not dcs:
            raise ProvisionError(f"profile {profile.name} has no data_center_ids to place its network volume")
        dc = dcs[0]
        progress(f"creating network volume {profile.storage.volume_name} ({profile.storage.size_gb} GB) in {dc}")
        vol = self.client.create_network_volume(profile.storage.volume_name, profile.storage.size_gb, dc)
        log_event("volume-created", profile=profile.name, volume_id=vol.id, name=vol.name,
                  data_center=dc, size_gb=vol.size_gb)
        return vol

    # ------------------------------------------------------------------ guards

    def check_guards(self, profile: Profile, opts: UpOptions) -> list[Pod]:
        live = [p for p in self.our_pods() if p.is_live]
        others = [p for p in live if p.name != profile.pod_name]
        gpus = sum(max(p.gpu_count, 1) for p in others) + profile.gpu_count
        limit = self.cfg.runpod.guards.max_gpu_count
        if others and not (opts.allow_concurrent or self.cfg.runpod.guards.allow_concurrent_profiles):
            names = ", ".join(f"{p.name} ({p.status}, ${p.cost_per_hr:.2f}/hr)" for p in others)
            raise GuardViolation(f"other qwenbench pods are live: {names}. Run `qwenbench down --all`, "
                                 "or pass --allow-concurrent (still subject to guards.max_gpu_count).")
        if gpus > limit:
            raise GuardViolation(f"starting {profile.name} would run {gpus} GPUs; guards.max_gpu_count={limit}")
        return [p for p in live if p.name == profile.pod_name]

    # ------------------------------------------------------------------ endpoint helpers

    def _urls(self, profile: Profile, pod: Pod) -> tuple[str | None, str | None]:
        if profile.endpoint_mode == "proxy":
            return pod.proxy_url(profile.port) + "/v1", pod.proxy_url(profile.watchdog_port)
        api = pod.tcp_url(profile.port)
        wd = pod.tcp_url(profile.watchdog_port)
        return (api + "/v1" if api else None), wd

    def supervisor_status(self, watchdog_url: str | None, api_key: str) -> dict[str, Any] | None:
        if not watchdog_url:
            return None
        try:
            with httpx.Client(timeout=10, transport=self._transport) as http:
                r = http.get(f"{watchdog_url}/status", headers={"Authorization": f"Bearer {api_key}"})
                return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    # ------------------------------------------------------------------ lifecycle

    def up(self, profile: Profile, opts: UpOptions | None = None, progress: Progress = print) -> Endpoint:
        opts = opts or UpOptions()
        profile = apply_options(profile, opts)
        existing = self.check_guards(profile, opts)
        session = load_session(profile.name)
        if existing:
            pod = existing[0]
            if session and session.pod_id == pod.id and session.ready_at:
                progress(f"{profile.name} already up: pod {pod.id} ({pod.status})")
                return self._endpoint_from_session(session)
            raise GuardViolation(f"pod {pod.id} named {pod.name} is live but has no ready local session; "
                                 f"run `qwenbench down {profile.name}` first")

        # Self-stop leaves the pod EXITED (the watchdog's key may stop but not
        # delete). Stopped pods bill nothing here, but clear them so they don't pile up.
        for stale in [p for p in self.pods_for(profile) if p.status == "EXITED"]:
            progress(f"removing stopped pod {stale.id} left by a previous session")
            self._terminate(stale.id, profile.name, reason="cleanup: stopped pod from a previous session")
        vol = self.ensure_volume(profile, progress)
        list_rate = self.cfg.pricing.gpu_rate(profile.gpu_type_id, profile.cloud) * profile.gpu_count
        api_key = pysecrets.token_urlsafe(32)
        env = podspec.pod_env(profile, endpoint_api_key=api_key, list_cost_per_hr=list_rate,
                              hf_token=get_secret("HF_TOKEN"),
                              self_stop_key=get_secret("RUNPOD_SELF_STOP_API_KEY"))
        # A volume pins the data center. Without one, try the preferred data
        # centers first, then let Runpod place the pod anywhere with stock.
        candidates = [vol.data_center] if vol else [*self.available_data_centers(profile), None]

        machine = ReadinessMachine(profile.startup_timeout_s, clock=self.clock)
        pod = self._create(profile, env, candidates, vol, progress)
        endpoint_url, watchdog_url = self._urls(profile, pod)
        session = Session(
            profile=profile.name, compute=self.name, pod_id=pod.id, pod_name=pod.name,
            endpoint_url=endpoint_url or "", watchdog_url=watchdog_url or "", api_key=api_key,
            served_model_name=profile.model.served_model_name, gpu_type_id=profile.gpu_type_id,
            cloud=profile.cloud, data_center_id=pod.data_center_id, network_volume_id=vol.id if vol else None,
            requested_at=machine.started_at, cost_per_hr=pod.cost_per_hr or list_rate,
            cost_source="runpod-pod-cost" if pod.cost_per_hr else "list-price",
            limits={"idle_timeout_s": profile.idle_timeout_s, "max_session_s": profile.max_session_s,
                    "max_spend_usd": profile.max_spend_usd, "startup_timeout_s": profile.startup_timeout_s},
            vllm_argv=podspec.vllm_argv(profile), config_fingerprint=config_fingerprint(profile),
        )
        save_session(session)  # before waiting: `qwenbench down` must work even if we are interrupted
        log_event("pod-created", profile=profile.name, pod_id=pod.id, gpu=profile.gpu_type_id,
                  data_center=pod.data_center_id, cost_per_hr=session.cost_per_hr, limits=session.limits)
        progress(f"pod {pod.id} requested ({profile.gpu_type_id}, {profile.cloud}, "
                 f"{pod.data_center_id or 'dc pending'}); idle timeout {format_duration(profile.idle_timeout_s)}")

        try:
            self._wait_ready(profile, session, machine, opts, progress)
        except BaseException as exc:  # includes KeyboardInterrupt
            reason = machine.failure or f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, KeyboardInterrupt):
                self._capture_failure_logs(session, progress)
            if opts.keep_on_failure:
                progress(f"startup failed ({reason}); --keep-on-failure set, pod {pod.id} left RUNNING (billing!)")
            else:
                progress(f"startup failed ({reason}); terminating pod {pod.id}")
                self._terminate(session.pod_id, profile.name, reason=f"startup-failed: {reason}")
                clear_session(profile.name)
            if isinstance(exc, ProvisionError):
                raise
            raise ProvisionError(reason) from exc
        return self._endpoint_from_session(session)

    def _capture_failure_logs(self, session: Session, progress: Progress, tail: int = 400) -> None:
        """Save the pod's logs before it is terminated; afterwards they are gone."""
        lines: list[str] = []
        try:
            with httpx.Client(timeout=15, transport=self._transport) as http:
                r = http.get(f"{session.watchdog_url}/logs", params={"n": tail},
                             headers={"Authorization": f"Bearer {session.api_key}"})
                if r.status_code == 200:
                    lines += ["# supervisor (vLLM output)", *r.json().get("lines", [])]
        except (httpx.HTTPError, ValueError):
            pass
        try:
            events = self.client.stream_logs(session.pod_id, tail=tail)
            lines += ["# runpod container/system logs",
                      *(f"[{e.get('source', '?')}] {e.get('line', '')}" for e in events)]
        except (RunpodError, httpx.HTTPError):
            pass
        if not lines:
            progress("could not retrieve pod logs before termination")
            return
        out = state_dir() / "failures" / f"{session.profile}-{session.pod_id}.log"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(redact("\n".join(lines), extra_secrets=[session.api_key]) + "\n")
        log_event("startup-logs-captured", profile=session.profile, pod_id=session.pod_id, path=str(out))
        progress(f"pod logs saved to {out}; last lines:")
        for line in [x for x in lines if not x.startswith("# ")][-25:]:
            progress(f"    {line}")

    def _create(self, profile: Profile, env: dict[str, str], dcs: list[str],
                vol: NetworkVolume | None, progress: Progress) -> Pod:
        errors = []
        for dc in dcs or [None]:
            body = podspec.create_body(profile, env=env, data_center_ids=[dc] if dc else [],
                                       network_volume_id=vol.id if vol else None)
            try:
                return self.client.create_pod(body)
            except RunpodError as e:
                if e.is_insufficient_balance:
                    raise ProvisionError(f"Runpod refused: insufficient balance ({e.detail})") from e
                if not e.is_capacity:
                    raise
                where = dc or "any data center"
                errors.append(f"{where}: {e.detail}")
                progress(f"no capacity for {profile.gpu_type_id} in {where}: {e.detail}")
        hint = (" The network volume pins the data center; retry later or use --storage ephemeral."
                if vol else "")
        raise ProvisionError(f"no capacity for {profile.gpu_type_id}: {'; '.join(errors)}.{hint}")

    def _wait_ready(self, profile: Profile, session: Session, machine: ReadinessMachine,
                    opts: UpOptions, progress: Progress) -> None:
        announced = Phase.REQUESTED
        while True:
            pod = self.client.get_pod(session.pod_id)
            obs = Observation(pod_status=pod.status if pod else "TERMINATED")
            if pod and pod.status == "RUNNING":
                endpoint_url, watchdog_url = self._urls(profile, pod)
                if (endpoint_url or "", watchdog_url or "") != (session.endpoint_url, session.watchdog_url):
                    session.endpoint_url, session.watchdog_url = endpoint_url or "", watchdog_url or ""
                    session.data_center_id = pod.data_center_id
                    save_session(session)
                sup = self.supervisor_status(session.watchdog_url, session.api_key)
                if sup:
                    obs.supervisor_phase = sup.get("phase")
                    if sup.get("cost_source") == "runpod-pod-cost":
                        session.cost_per_hr, session.cost_source = sup["cost_per_hr"], "runpod-pod-cost"
                if session.endpoint_url and (obs.supervisor_phase in ("server_up", None)):
                    model = OpenAICompatibleModel(self._endpoint_from_session(session), transport=self._transport)
                    h = model.health()
                    obs.health_ok = h["health_ok"] and h["model_listed"]
                    if obs.health_ok:
                        probe = model.probe()
                        obs.probe_ok, obs.probe_error = probe["ok"], probe.get("error")
            phase = machine.observe(obs)
            if phase != announced:
                for t in machine.history:
                    if t.phase > announced and t.phase <= phase or t.phase.terminal and t.phase == phase:
                        tag = " (inferred)" if t.inferred else ""
                        progress(f"  [{t.at - machine.started_at:7.1f}s] {LABELS[t.phase]}{tag}"
                                 + (f": {t.detail}" if t.detail else ""))
                announced = phase
            if phase == Phase.READY:
                session.ready_at = self.clock()
                session.startup = machine.timings()
                save_session(session)
                log_event("pod-ready", profile=profile.name, pod_id=session.pod_id,
                          startup_s=round(session.ready_at - session.requested_at, 1), phases=session.startup)
                self._warn_if_pod_cannot_stop_itself(session, progress)
                return
            if phase in (Phase.FAILED, Phase.TIMED_OUT):
                raise ProvisionError(machine.failure or phase.name)
            self.sleep(opts.poll_s)

    def _warn_if_pod_cannot_stop_itself(self, session: Session, progress: Progress) -> None:
        sup = self.supervisor_status(session.watchdog_url, session.api_key) or {}
        if sup.get("runpod_api_auth_ok") is not False:
            return
        source, code = sup.get("runpod_api_key_source"), sup.get("runpod_api_probe_status")
        log_event("self-stop-unverified", profile=session.profile, pod_id=session.pod_id, key_source=source,
                  probe_status=code)
        if source == "self-stop-key":
            progress(f"note: the pod could not read its own record (HTTP {code}); in-pod self-stop is unverified "
                     "but has worked with a self-stop key before. The local guard remains the backstop.")
        else:
            progress(f"WARNING: no RUNPOD_SELF_STOP_API_KEY and Runpod's pod key was refused (HTTP {code}); the pod "
                     "cannot stop itself. Only the local guard can, and only while this machine is awake. "
                     "See docs/runpod-setup.md.")

    def _terminate(self, pod_id: str, profile: str, reason: str) -> bool:
        gone = not self.client.terminate_pod(pod_id)
        log_event("pod-terminated", profile=profile, pod_id=pod_id, reason=reason, already_gone=gone)
        return not gone

    def down(self, profile: Profile, reason: str = "user: qwenbench down") -> list[str]:
        """Idempotent. Terminates the session's pod and any pod carrying the profile's name."""
        session = load_session(profile.name)
        ids = {p.id for p in self.pods_for(profile)}
        if session:
            ids.add(session.pod_id)
            self._record_final_cost(session)
        terminated = [pid for pid in sorted(ids) if self._terminate(pid, profile.name, reason)]
        clear_session(profile.name)
        return terminated

    def down_all(self, reason: str = "user: qwenbench down --all") -> list[str]:
        terminated = []
        for pod in self.our_pods():
            profile = pod.name.removeprefix(self.cfg.runpod.api.pod_name_prefix)
            s = load_session(profile)
            if s:
                self._record_final_cost(s)
            if self._terminate(pod.id, profile, reason):
                terminated.append(pod.id)
        for name in self.cfg.profile_names():
            clear_session(name)
        return terminated

    def _record_final_cost(self, session: Session) -> None:
        start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(session.requested_at - 3600))
        try:
            billing = self.client.pod_billing(session.pod_id, start)
            totals = (billing.get("metadata") or {}).get("totals") or {}
            log_event("pod-billing", profile=session.profile, pod_id=session.pod_id, source="runpod-billing-api",
                      totals=totals)
        except (RunpodError, httpx.HTTPError):
            pass

    def status(self, profile: Profile) -> ComputeStatus:
        session = load_session(profile.name)
        pods = self.pods_for(profile)
        pod = next((p for p in pods if session and p.id == session.pod_id), pods[0] if pods else None)
        if not pod:
            return ComputeStatus(profile.name, False, "NONE", False, None, None, None,
                                 {"stale_session": bool(session)})
        rate = pod.cost_per_hr or (session.cost_per_hr if session else None)
        started = pod.started_at.timestamp() if pod.started_at else (session.requested_at if session else None)
        uptime = (self.clock() - started) if (started and pod.is_live) else (pod.uptime_s or None)
        spend = (rate * uptime / 3600) if (rate and uptime) else None
        detail: dict[str, Any] = {"pod_id": pod.id, "data_center": pod.data_center_id, "gpu": pod.gpu_type_id}
        if session:
            detail["endpoint"] = session.endpoint_url
            detail["supervisor"] = self.supervisor_status(session.watchdog_url, session.api_key) if pod.is_live else None
        return ComputeStatus(profile.name, True, pod.status, pod.is_live, rate, uptime, spend, detail)

    def logs(self, profile: Profile, tail: int = 200, follow: bool = False) -> Iterator[str]:
        session = load_session(profile.name)
        pods = self.pods_for(profile)
        pod_id = session.pod_id if session else (pods[0].id if pods else None)
        if not pod_id:
            raise ProvisionError(f"no pod for profile {profile.name}")
        try:
            for ev in self.client.stream_logs(pod_id, tail=tail, follow=follow):
                yield f"[{ev.get('source', '?')}] {ev.get('line', '')}"
            return
        except RunpodError as e:
            yield f"(runpod logs API unavailable: {e}; falling back to the in-pod supervisor)"
        if session:
            with httpx.Client(timeout=15, transport=self._transport) as http:
                r = http.get(f"{session.watchdog_url}/logs", params={"n": tail},
                             headers={"Authorization": f"Bearer {session.api_key}"})
                if r.status_code == 200:
                    yield from r.json().get("lines", [])

    def endpoint(self, profile: Profile) -> Endpoint | None:
        session = load_session(profile.name)
        return self._endpoint_from_session(session) if session and session.ready_at else None

    def _endpoint_from_session(self, s: Session) -> Endpoint:
        return Endpoint(
            profile=s.profile, base_url=s.endpoint_url, api_key=s.api_key, model=s.served_model_name,
            metadata={"compute": s.compute, "pod_id": s.pod_id, "gpu_type_id": s.gpu_type_id,
                      "cloud": s.cloud, "data_center_id": s.data_center_id,
                      "config_fingerprint": s.config_fingerprint},
        )

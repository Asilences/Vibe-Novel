import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { CircleAlert, KeyRound, LoaderCircle, RefreshCw, Save, ShieldCheck } from "lucide-react";
import { api, ApiError } from "./api";
import type { APISettingsData } from "./api";
import { Modal } from "./ui";

export default function APISettings({ onClose, onSaved, onModels }: { onClose: () => void; onSaved: () => Promise<void>; onModels: () => void }) {
  const [settings, setSettings] = useState<APISettingsData | null>(null);
  const [latestSettings, setLatestSettings] = useState<APISettingsData | null>(null);
  const [preset, setPreset] = useState("custom");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [inputPrice, setInputPrice] = useState("");
  const [outputPrice, setOutputPrice] = useState("");
  const [secret, setSecret] = useState("");
  const secretRef = useRef(secret);
  secretRef.current = secret;
  const [clearKey, setClearKey] = useState(false);
  const [allowLoopback, setAllowLoopback] = useState(false);
  const [loading, setLoading] = useState(true);
  const [multiModelActive, setMultiModelActive] = useState(false);
  const [saving, setSaving] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const alive = useRef(true);
  const loadRequest = useRef(0);
  function fillPublicFields(value: APISettingsData) {
    setBaseUrl(value.api.base_url);
    setModel(value.api.model);
    setInputPrice(value.api.price_input_usd == null ? "" : String(value.api.price_input_usd));
    setOutputPrice(value.api.price_output_usd == null ? "" : String(value.api.price_output_usd));
    setAllowLoopback(value.api.allow_loopback);
    setPreset(value.presets.find((item) => item.base_url === value.api.base_url)?.id || "custom");
  }
  useEffect(() => {
    alive.current = true;
    const request = ++loadRequest.current;
    void Promise.all([api<APISettingsData>("/settings"), api<{ migration_required: boolean }>("/model-registry")]).then(([value, registry]) => {
      if (!alive.current || request !== loadRequest.current) return;
      setSettings(value);
      setMultiModelActive(!registry.migration_required);
      fillPublicFields(value);
    }).catch(() => {
      if (alive.current && request === loadRequest.current) setError("无法读取本机 API 设置，请稍后重试。");
    }).finally(() => { if (alive.current && request === loadRequest.current) setLoading(false); });
    return () => { alive.current = false; loadRequest.current += 1; };
  }, []);
  const selectedPreset = settings?.presets.find((item) => item.id === preset);
  const endpointNeedsKey = !!settings?.api.has_api_key && baseUrl.trim() !== settings.api.base_url && !secret.trim() && !clearKey;
  const busy = saving || reloading;
  function clearPrices() {
    setInputPrice(""); setOutputPrice(""); setNotice("");
  }
  async function reload() {
    if (busy) return;
    const request = ++loadRequest.current;
    setReloading(true); setError(""); setNotice("");
    try {
      const [value, registry] = await Promise.all([api<APISettingsData>("/settings"), api<{ migration_required: boolean }>("/model-registry")]);
      if (!alive.current || request !== loadRequest.current) return;
      setMultiModelActive(!registry.migration_required);
      if (!settings) { setSettings(value); fillPublicFields(value); }
      else if (value.hash !== settings.hash) setLatestSettings(value);
      else { setLatestSettings(null); setNotice("本机配置没有变化，当前输入已保留。"); }
    } catch {
      if (alive.current && request === loadRequest.current) setError("无法重新读取本机配置；当前输入已保留，请稍后重试。");
    } finally { if (alive.current && request === loadRequest.current) setReloading(false); }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!settings || busy || latestSettings || multiModelActive) return;
    if (endpointNeedsKey) { setError("更换 API 地址时，请填写该服务的密钥，或勾选清除原密钥。"); return; }
    const prices = [inputPrice, outputPrice].map((value) => value.trim() === "" ? null : Number(value));
    if (prices.some((value) => value !== null && (!Number.isFinite(value) || value < 0 || value > 1_000_000))) {
      setError("价格需填写 0 到 1000000 之间的美元数值，或留空表示未知。"); return;
    }
    const submittedSecret = secret;
    setSaving(true);
    setNotice("");
    setError("");
    try {
      const value = await api<APISettingsData>("/settings", "PUT", {
        expected_hash: settings.hash,
        api: { base_url: baseUrl.trim(), model: model.trim(), allow_loopback: allowLoopback,
          price_input_usd: prices[0], price_output_usd: prices[1],
          ...(clearKey ? { clear_api_key: true } : submittedSecret ? { api_key: submittedSecret } : {}) },
      });
      if (alive.current) {
        setSettings(value);
        if (secretRef.current === submittedSecret) setSecret("");
        setClearKey(false);
        setNotice("设置已保存。");
      }
      // Saving has completed. A subsequent status refresh must not make the
      // user think the credential needs to be submitted a second time.
      try {
        await onSaved();
        if (alive.current) setNotice("设置已保存，API 配置状态已刷新；尚未测试连接。");
      } catch {
        if (alive.current) setNotice("设置已保存，但工作台状态暂未刷新。关闭设置后刷新页面即可，无需再次提交密钥。");
      }
    } catch (failure) {
      // Never echo provider/server error text: it could contain submitted
      // credentials. No key, even a fragment, leaves this password field.
      if (alive.current) setError(failure instanceof ApiError && failure.status === 409
        ? "本机配置已被其他窗口或程序更新，本次未保存。请先重新读取配置，核对变化后继续；当前输入已保留。"
        : failure instanceof ApiError && failure.status === 403
          ? "保存请求未通过本机会话校验。请关闭设置并刷新页面后重试。"
          : "设置未保存。请检查接口地址、模型、价格和密钥；外部接口需使用 HTTPS，本机接口需勾选允许本机回环地址。");
    } finally { if (alive.current) setSaving(false); }
  }
  return <Modal title="模型与 API 设置" subtitle="生成任务统一通过 API 执行。配置保存在本机，不会自动测试连通性。" onClose={onClose}>
    {loading ? <div className="analysis-loading"><LoaderCircle size={20} className="spin" />读取配置状态…</div> : multiModelActive ? <div className="modal-form api-settings"><div className="analysis-warning"><ShieldCheck size={16} /><span>本机已启用多模型注册表。当前任务使用“多模型协作”中保存的模型和作品角色绑定，请在那里修改密钥与模型配置。</span></div><p className="form-note">原单 API 配置保留供回顾；修改它不会更新当前任务路线。</p><div className="modal-footer"><button type="button" className="button secondary" onClick={onClose}>关闭</button><button type="button" className="button primary" onClick={onModels}>打开多模型协作</button></div></div> : settings ? <form className="modal-form api-settings" onSubmit={(event) => void save(event)}>
      <div className="api-key-status"><KeyRound size={16} /><strong>{settings.api.has_api_key ? "已配置密钥" : "尚未配置密钥"}</strong><span>已保存的密钥不会回显</span></div>
      <label>服务商<select value={preset} disabled={busy} onChange={(event) => {
        const id = event.target.value;
        setPreset(id); clearPrices();
        const item = settings.presets.find((entry) => entry.id === id);
        if (item) { setBaseUrl(item.base_url); setModel(item.models[0]?.id || ""); }
      }}>{!settings.presets.some((item) => item.id === "custom") && <option value="custom">自定义兼容服务</option>}{settings.presets.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>Base URL<input type="url" required value={baseUrl} onChange={(event) => { setBaseUrl(event.target.value); clearPrices(); }} placeholder="https://api.example.com/v1" disabled={busy} /></label>
      {selectedPreset && selectedPreset.models.length > 0 && <label>预设模型<select value={selectedPreset.models.some((item) => item.id === model) ? model : ""} disabled={busy} onChange={(event) => { setModel(event.target.value); clearPrices(); }}><option value="">自定义模型 ID</option>{selectedPreset.models.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.id}</option>)}</select></label>}
      <label>模型 ID<input required value={model} onChange={(event) => { setModel(event.target.value); clearPrices(); }} placeholder="填写模型 ID；豆包可填写 ep-… 接入点" disabled={busy} maxLength={500} /></label>
      <label>输入价格（美元／百万 Token）<input type="number" min="0" max="1000000" step="any" value={inputPrice} onChange={(event) => { setInputPrice(event.target.value); setNotice(""); }} placeholder="按服务商未命中缓存的输入报价填写；留空表示未知" disabled={busy} /></label>
      <label>输出价格（美元／百万 Token）<input type="number" min="0" max="1000000" step="any" value={outputPrice} onChange={(event) => { setOutputPrice(event.target.value); setNotice(""); }} placeholder="按服务商的输出报价填写；留空表示未知" disabled={busy} /></label>
      <p className="form-note">整书研究的美元预算需要以上两项价格。请按当前模型的美元报价填写，人民币报价需先换算；不自动猜价。更换接口或模型后需重新填写。</p>
      <label>API Key<input type="password" autoComplete="new-password" spellCheck={false} value={secret} onChange={(event) => { setSecret(event.target.value); setNotice(""); }} disabled={clearKey || busy} placeholder={endpointNeedsKey ? "更换地址后需输入对应服务的密钥" : settings.api.has_api_key ? "地址不变时，留空保留已保存密钥" : "输入新密钥"} /></label>
      {endpointNeedsKey && <div className="analysis-warning"><CircleAlert size={15} /><span>API 地址已改变。请填写新地址对应的密钥，或勾选下方“清除本机已保存的密钥”后保存。</span></div>}
      <label className="analysis-checkbox"><input type="checkbox" checked={clearKey} disabled={busy} onChange={(event) => { setClearKey(event.target.checked); setNotice(""); }} />清除本机已保存的密钥</label>
      <label className="analysis-checkbox"><input type="checkbox" checked={allowLoopback} disabled={busy} onChange={(event) => { setAllowLoopback(event.target.checked); setNotice(""); }} />允许本机回环地址（用于本地兼容服务）</label>
      {settings.environment_overrides.length > 0 && <div className="analysis-warning"><CircleAlert size={15} /><span>以下设置正在被启动时的环境变量覆盖：{settings.environment_overrides.map((field) => ({ base_url: "接口地址", model: "模型", api_key: "密钥", allow_loopback: "本机接口开关" })[field as "base_url" | "model" | "api_key" | "allow_loopback"] || field).join("、")}。保存配置不会移除这些环境变量。若实际接口或模型不同，已保存报价不会用于该路线。</span></div>}
      <p className="form-note"><ShieldCheck size={14} />这里只保存设置；开始生成后，服务商可能按用量计费。同一地址下，留空密钥会保留原值。</p>
      {latestSettings && <div className="api-settings-conflict" role="status">
        <strong>本机已有更新的配置，请先核对</strong>
        <p>接口地址：{latestSettings.api.base_url || "未填写"}<br />模型：{latestSettings.api.model || "未填写"}<br />输入／输出价格：{latestSettings.api.price_input_usd ?? "未知"} ／ {latestSettings.api.price_output_usd ?? "未知"} 美元／百万 Token<br />密钥：{latestSettings.api.has_api_key ? "已配置（不回显）" : "未配置"}</p>
        <p>你的输入已保留。选择继续后，下次保存将以本机最新版本为基础。</p>
        <div className="api-settings-conflict-actions"><button type="button" className="button secondary small" disabled={busy} onClick={() => { setSettings(latestSettings); setLatestSettings(null); setError(""); setNotice("已采用最新配置版本，保留你的输入。请核对后点击保存配置。"); }}>保留我的输入并继续</button><button type="button" className="button secondary small" disabled={busy} onClick={() => { setSettings(latestSettings); fillPublicFields(latestSettings); setLatestSettings(null); setClearKey(false); setError(""); setNotice("已载入最新地址与模型，尚未提交的密钥输入仍保留。"); }}>载入最新地址与模型</button></div>
      </div>}
      {notice && <p className="analysis-success" role="status">{notice}</p>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="modal-footer"><button type="button" className="button secondary" disabled={busy} onClick={() => void reload()}><RefreshCw size={15} className={reloading ? "spin" : ""} />重新读取配置</button><button type="button" className="button secondary" onClick={onClose}>关闭</button><button type="submit" className="button primary" disabled={busy || !!latestSettings || endpointNeedsKey || !baseUrl.trim() || !model.trim()}>{saving ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}保存配置</button></div>
    </form> : <><p className="inline-error" role="alert">{error}</p><button type="button" className="button secondary" disabled={reloading} onClick={() => void reload()}><RefreshCw size={15} className={reloading ? "spin" : ""} />重新读取配置</button></>}
  </Modal>;
}

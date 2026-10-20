import type { ModelProfile } from "./api";

export function selectionChanged(selected: string[], saved: string[]): boolean {
  const left = [...new Set(selected)].sort();
  const right = [...new Set(saved)].sort();
  return JSON.stringify(left) !== JSON.stringify(right);
}

function editableProfile(profile: ModelProfile) {
  return {
    id: profile.id, label: profile.label, base_url: profile.base_url, model: profile.model,
    allow_loopback: profile.allow_loopback, roles: [...profile.roles].sort(),
    context_tokens: profile.context_tokens, max_output_tokens: profile.max_output_tokens,
    price_input_usd: profile.price_input_usd, price_output_usd: profile.price_output_usd,
  };
}

// The saved key is never available to the browser. Presence of a newly typed key
// or an explicit removal is itself an edit, independently of public fields.
export function modelProfileChanged(draft: ModelProfile, saved: ModelProfile, hasNewKey: boolean, clearKey: boolean): boolean {
  return hasNewKey || clearKey || JSON.stringify(editableProfile(draft)) !== JSON.stringify(editableProfile(saved));
}

export type ResearchConfig = { model_id: string; max_cost_usd: number; max_calls: number; price_input_usd: number; price_output_usd: number; price_source: string };

export function researchModelDraft(current: ResearchConfig, modelId: string): ResearchConfig {
  // A quote belongs to its model. A changed destination requires a new explicit quote.
  return current.model_id === modelId ? current : { ...current, model_id: modelId, price_input_usd: 0, price_output_usd: 0, price_source: "" };
}

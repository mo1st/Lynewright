/**
 * The provider types the fallback-chain editor offers, and their display
 * names. Shared by the chain list and the profile form so the two can never
 * disagree about what a route is called.
 */
export const AI_PROVIDERS = [
  "mock",
  "codex",
  "gemini_cli",
  "gemini",
  "vertex",
  "openai",
  "openai_compatible",
  "anthropic",
  "grok",
  "mistral",
  "deepseek"
];

export const PROVIDER_LABELS: Record<string, string> = {
  mock: "Deterministic mock",
  codex: "OpenAI Codex (ChatGPT account)",
  gemini_cli: "Google Gemini CLI (Google account)",
  gemini: "Google Gemini",
  vertex: "Google Vertex AI",
  openai: "OpenAI (ChatGPT)",
  openai_compatible: "OpenAI-compatible (generic)",
  anthropic: "Anthropic (Claude)",
  grok: "Grok (xAI)",
  mistral: "Mistral AI",
  deepseek: "DeepSeek"
};

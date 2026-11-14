export function voiceThresholdPercent(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined;
  return value <= 1 ? value * 100 : value;
}

export function updateVoiceSettings(settings: Record<string, unknown>, values: Record<string, unknown>) {
  const existing = (settings.voice ?? {}) as Record<string, unknown>;
  const next = { ...values };
  if (typeof next.low_confidence_threshold === 'number') {
    next.low_confidence_threshold /= 100;
  }
  return { ...settings, voice: { ...existing, ...next } };
}

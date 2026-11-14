import { describe, expect, it } from 'vitest';
import { updateVoiceSettings, voiceThresholdPercent } from './voice-settings-values';

describe('voice settings confidence units', () => {
  it('displays fractional thresholds as percentages including the upper boundary', () => {
    expect(voiceThresholdPercent(0.5)).toBe(50);
    expect(voiceThresholdPercent(1)).toBe(100);
    expect(voiceThresholdPercent(0)).toBe(0);
    expect(voiceThresholdPercent(65)).toBe(65);
    expect(voiceThresholdPercent(undefined)).toBeUndefined();
  });

  it('saves backend fractional thresholds while preserving unrelated and hidden settings', () => {
    const settings = { escalation: { calls_enabled: true }, voice: { low_confidence_threshold: 0.5, max_turns: 30 } };
    expect(updateVoiceSettings(settings, { low_confidence_threshold: 70, recording_enabled: true })).toEqual({
      escalation: { calls_enabled: true }, voice: { low_confidence_threshold: 0.7, max_turns: 30, recording_enabled: true },
    });
    expect(settings.voice.low_confidence_threshold).toBe(0.5);
  });

  it('normalizes legacy percentage representation and defaults missing settings to fractions', () => {
    expect(updateVoiceSettings({ voice: { low_confidence_threshold: 60 } }, { low_confidence_threshold: 75 }).voice.low_confidence_threshold).toBe(0.75);
    expect(updateVoiceSettings({}, { low_confidence_threshold: 75 }).voice.low_confidence_threshold).toBe(0.75);
  });
});

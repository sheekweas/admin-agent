import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { VoiceNote } from './voice-note';

it('renders voice transcript, mic badge and confidence', () => {
  render(<VoiceNote message={{message_type:'voice_note',text:'Нужна запись',meta:{transcribed:true,stt_confidence:0.92}}}/>);
  expect(screen.getByText('Голосовое сообщение')).toBeInTheDocument();
  expect(screen.getByText('Нужна запись')).toBeInTheDocument();
  expect(screen.getByText(/92%/)).toBeInTheDocument();
});
it('uses the tenant threshold and explicit low-confidence marker', () => {
  const { rerender } = render(<VoiceNote lowConfidenceThreshold={0.5} message={{message_type:'audio',text:'Текст',meta:{transcribed:true,stt_confidence:0.65}}}/>);
  expect(screen.queryByText(/Низкая точность/)).not.toBeInTheDocument();
  rerender(<VoiceNote lowConfidenceThreshold={0.5} message={{message_type:'audio',text:'Текст',meta:{transcribed:true,stt_confidence:0.65,stt_low_confidence:true}}}/>);
  expect(screen.getByText(/Низкая точность/)).toHaveClass('text-amber-800');
});
it.each([{transcribed:false}, {stt_error:'provider timeout'}])('hides unrecognized transcript for failure %j', meta => {
  render(<VoiceNote message={{message_type:'voice_note',text:'[audio]',meta}}/>);
  expect(screen.getByText('Не распознано')).toBeInTheDocument();
  expect(screen.queryByText('[audio]')).not.toBeInTheDocument();
});
it('keeps call turns as normal transcript text', () => {
  render(<VoiceNote message={{message_type:'voice_turn',text:'Call text',meta:{transcribed:true}}}/>);
  expect(screen.getByText('Call text')).toBeInTheDocument();
  expect(screen.queryByText('Голосовое сообщение')).not.toBeInTheDocument();
});

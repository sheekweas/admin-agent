import {render,screen} from '@testing-library/react';
import {describe,it,expect} from 'vitest';
import {I18nProvider} from '@/lib/i18n';
import {StatusBadge} from './status-badge';
describe('status indicators',()=>{it('shows a textual confirmation as well as color',()=>{render(<I18nProvider><StatusBadge status="confirmed"/></I18nProvider>);expect(screen.getByText('бронь подтверждена')).toHaveClass('badge-success');});it('localizes a delivery error in Kazakh',()=>{localStorage.setItem('crm:lang','kk');render(<I18nProvider><StatusBadge status="failed"/></I18nProvider>);expect(screen.getByText('Жеткізу қатесі')).toHaveClass('badge-danger');});it('keeps unknown statuses visible',()=>{render(<StatusBadge status="custom_state"/>);expect(screen.getByText('custom_state')).toBeVisible();});});

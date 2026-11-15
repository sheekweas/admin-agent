'use client';
import {useState, type FormEvent} from 'react';
import {useQueryClient} from '@tanstack/react-query';
import {api,ApiError,notify} from '@/lib/api';
import {useApi} from '@/hooks/use-api';
import {useI18n} from '@/lib/i18n';
import {useSession} from '@/lib/session';
import {formatDate} from '@/lib/time';
import type {AuthSession,Page,PasswordChangeResult} from '@/lib/types';
import {Badge,Button,Card,ConfirmButton,EmptyState,Input,QueryState,Table} from '@/components/ui';

export default function SecurityPage(){
  const {t}=useI18n();
  const {timezone,clearLocal}=useSession();
  const client=useQueryClient();
  const sessions=useApi<Page<AuthSession>>('/v1/auth/sessions');
  const [current,setCurrent]=useState(''),[password,setPassword]=useState(''),[confirm,setConfirm]=useState('');
  const [pending,setPending]=useState(false),[ending,setEnding]=useState<string|null>(null),[error,setError]=useState('');
  async function endSession(session:AuthSession){
    setEnding(session.id);
    try{await api<void>(`/v1/auth/sessions/${encodeURIComponent(session.id)}`,{method:'DELETE'});if(session.current){clearLocal();return;}await client.invalidateQueries({queryKey:['/v1/auth/sessions']});notify(t('Сессия завершена','Сессия аяқталды'));}catch{/* API displays the error; retain the row for retry. */}finally{setEnding(null);}
  }
  async function changePassword(event:FormEvent<HTMLFormElement>){
    event.preventDefault();setError('');
    if(password.length<10||/^\d+$/.test(password)){setError(t('Пароль должен содержать не менее 10 символов и не состоять только из цифр','Құпиясөз кемінде 10 таңбадан тұруы және тек сандардан құралмауы керек'));return;}
    if(password!==confirm){setError(t('Пароли не совпадают','Құпиясөздер сәйкес келмейді'));return;}
    setPending(true);
    try{const result=await api<PasswordChangeResult>('/v1/auth/change-password',{method:'POST',body:JSON.stringify({current_password:current,new_password:password}),silent:true});setCurrent('');setPassword('');setConfirm('');await client.invalidateQueries({queryKey:['/v1/auth/sessions']});notify(`${t('Пароль изменён. Завершено других сессий:','Құпиясөз өзгертілді. Аяқталған басқа сессиялар:')} ${result.revoked_other_sessions}`);}
    catch(cause){setError(cause instanceof ApiError&&cause.code==='INVALID_CURRENT_PASSWORD'?t('Текущий пароль неверен','Ағымдағы құпиясөз қате'):cause instanceof ApiError&&cause.code==='WEAK_PASSWORD'?t('Пароль должен содержать не менее 10 символов и не состоять только из цифр','Құпиясөз кемінде 10 таңбадан тұруы және тек сандардан құралмауы керек'):t('Не удалось изменить пароль. Попробуйте снова.','Құпиясөзді өзгерту мүмкін болмады. Қайталап көріңіз.'));}
    finally{setPending(false);}
  }
  return <div className="grid gap-6">
    <div className="page-header"><div><h1>{t('Безопасность','Қауіпсіздік')}</h1><p>{t('Управляйте сессиями и паролем своего аккаунта','Аккаунтыңыздың сессиялары мен құпиясөзін басқарыңыз')}</p></div></div>
    <Card><div className="page-header"><h2>{t('Активные сессии','Белсенді сессиялар')}</h2><ConfirmButton variant="danger" title={t('Выйти на всех устройствах?','Барлық құрылғылардан шығу керек пе?')} description={t('Все сессии будут завершены, включая текущую. Потребуется войти снова.','Барлық сессиялар, соның ішінде ағымдағы сессия аяқталады. Қайта кіру қажет болады.')} onConfirm={async()=>{await api('/v1/auth/logout-all',{method:'POST'});clearLocal();}}>{t('Выйти на всех устройствах','Барлық құрылғылардан шығу')}</ConfirmButton></div>
      <QueryState query={sessions}>{sessions.data?.items.length?<Table><caption className="sr-only">{t('Активные сессии','Белсенді сессиялар')}</caption><thead><tr><th scope="col">{t('Устройство','Құрылғы')}</th><th scope="col">IP</th><th scope="col">{t('Создана','Құрылған')}</th><th scope="col">{t('Последняя активность','Соңғы белсенділік')}</th><th scope="col">{t('Действия','Әрекеттер')}</th></tr></thead><tbody>{sessions.data.items.map(session=><tr key={session.id}><td><div className="max-w-xs break-words whitespace-normal">{session.user_agent||t('Неизвестное устройство','Белгісіз құрылғы')}</div>{session.current&&<Badge variant="success">{t('Это устройство','Осы құрылғы')}</Badge>}</td><td>{session.ip||'—'}</td><td>{formatDate(session.created_at,timezone)}</td><td>{formatDate(session.last_seen_at,timezone)}</td><td><Button variant="secondary" disabled={ending!==null} onClick={()=>void endSession(session)} aria-label={`${t('Завершить сессию','Сессияны аяқтау')} ${session.user_agent||session.ip||session.id}`}>{ending===session.id?t('Завершение…','Аяқталуда…'):t('Завершить сессию','Сессияны аяқтау')}</Button></td></tr>)}</tbody></Table>:<EmptyState title={t('Нет активных сессий','Белсенді сессиялар жоқ')}/>}</QueryState>
    </Card>
    <Card><h2>{t('Изменить пароль','Құпиясөзді өзгерту')}</h2><form onSubmit={changePassword} className="max-w-lg"><label className="field">{t('Текущий пароль','Ағымдағы құпиясөз')}<Input type="password" autoComplete="current-password" required value={current} onChange={event=>setCurrent(event.target.value)}/></label><label className="field">{t('Новый пароль','Жаңа құпиясөз')}<Input type="password" autoComplete="new-password" required minLength={10} value={password} onChange={event=>setPassword(event.target.value)} aria-describedby="password-help"/></label><p id="password-help" className="muted">{t('Не менее 10 символов, не только цифры','Кемінде 10 таңба, тек сандардан тұрмауы керек')}</p><label className="field">{t('Подтвердите новый пароль','Жаңа құпиясөзді растаңыз')}<Input type="password" autoComplete="new-password" required value={confirm} onChange={event=>setConfirm(event.target.value)}/></label>{error&&<p role="alert" className="form-error">{error}</p>}<Button disabled={pending}>{pending?t('Сохранение…','Сақталуда…'):t('Изменить пароль','Құпиясөзді өзгерту')}</Button></form></Card>
  </div>;
}

'use client';
import {createContext,useContext,useEffect,useState,type ReactNode} from 'react';
import {dictionary} from './locales';
const Context=createContext({lang:'ru' as 'ru'|'kk',setLang:(_lang:'ru'|'kk')=>{},t:(ru:string,_kk?:string)=>ru});
export function I18nProvider({children}:{children:ReactNode}) {const [lang,setLanguage]=useState<'ru'|'kk'>('ru');useEffect(()=>{const saved=localStorage.getItem('crm:lang')==='kk'?'kk':'ru';setLanguage(saved);document.documentElement.lang=saved;},[]);function setLang(value:'ru'|'kk'){setLanguage(value);localStorage.setItem('crm:lang',value);document.documentElement.lang=value;}return <Context.Provider value={{lang,setLang,t:(ru,kk)=>lang==='kk'?(dictionary[ru]||kk||ru):ru}}>{children}</Context.Provider>;}
export const useI18n=()=>useContext(Context);

import { FlatCompat } from '@eslint/eslintrc';
const compat = new FlatCompat({baseDirectory: import.meta.dirname});
const config = [...compat.extends('next/core-web-vitals', 'next/typescript'), {rules:{'@typescript-eslint/no-unused-vars':['error',{argsIgnorePattern:'^_'}]}}, { ignores: ['.next/**', 'node_modules/**', '.npm-cache/**', '.playwright/**', 'playwright-report/**', 'test-results*/**', 'coverage/**', 'next-env.d.ts'] }];
export default config;

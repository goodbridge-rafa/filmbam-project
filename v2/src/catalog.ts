// Catalog + prices. Must stay IDENTICAL to the earlier console (MENU, CINEMA, p90).
// Prices always end in .90; `cost` is the estimate (typical measured cost) used against the caps.
import type { Caps, Look, Mode } from './types';

export interface MenuItem {
  v: number;
  l: string;
  price: number;
  cost: number;
}

export const MENU: Record<Mode, readonly MenuItem[]> = {
  film: [
    { v: 5, l: '5 s', price: 5.9, cost: 2.7 },
    { v: 10, l: '10 s', price: 9.9, cost: 4.5 },
    { v: 20, l: '20 s', price: 17.9, cost: 8.3 },
    { v: 30, l: '30 s', price: 24.9, cost: 12 },
  ],
  story: [
    { v: 6, l: '6 frames', price: 2.9, cost: 1.1 },
    { v: 12, l: '12 frames', price: 4.9, cost: 1.6 },
  ],
};

export const CINEMA = 1.5;
export const MODES: readonly Mode[] = ['film', 'story'];
export const FMTS: readonly string[] = ['9:16', '16:9', '1:1'];
export const LOOKS: readonly Look[] = ['standard', 'cinema'];

/** Same function as the console: rounds to a price ending in .90 (minimum 0.90). */
export function p90(n: number): number {
  return Math.max(0.9, Math.round(n - 0.9) + 0.9);
}

export function isMode(v: unknown): v is Mode {
  return v === 'film' || v === 'story';
}

export function isLook(v: unknown): v is Look {
  return v === 'standard' || v === 'cinema';
}

export function isFmt(v: unknown): v is string {
  return typeof v === 'string' && FMTS.includes(v);
}

export function lookup(mode: Mode, len: number): MenuItem | null {
  return MENU[mode].find((i) => i.v === len) ?? null;
}

export function mult(q: Look): number {
  return q === 'cinema' ? CINEMA : 1;
}

/** The console's priceNow(). */
export function priceFor(item: MenuItem, q: Look): number {
  return p90(item.price * mult(q));
}

/** The console's costNow(). */
export function costFor(item: MenuItem, q: Look): number {
  return +(item.cost * mult(q)).toFixed(2);
}

/** Per-order cap by mode (default US$ 30 film / US$ 5 storyboard). */
export function perOrderCap(mode: Mode, caps: Caps): number {
  return mode === 'film' ? caps.film : caps.story;
}

/** GET /api/catalog response, so the front end renders exactly the same numbers. */
export function catalogJson(caps: Caps) {
  return {
    menu: MENU,
    cinema: CINEMA,
    fmts: FMTS,
    looks: LOOKS,
    linkDays: caps.linkDays,
    caps: { perOrderFilm: caps.film, perOrderStory: caps.story, month: caps.month, perDay: caps.perDay },
  };
}

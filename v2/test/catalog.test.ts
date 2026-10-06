import { describe, expect, it } from 'vitest';
import { CINEMA, MENU, costFor, lookup, p90, priceFor } from '../src/catalog';

describe('catalog = console MENU', () => {
  it('p90 matches the console function', () => {
    expect(p90(0.1)).toBe(0.9);
    expect(p90(5.9)).toBe(5.9);
    expect(p90(24.9 * 1.5)).toBe(36.9);
    expect(p90(2.9 * 1.5)).toBe(3.9);
  });

  it('standard prices are the menu prices; cinema is ×1.5 then p90', () => {
    for (const mode of ['film', 'story'] as const) {
      for (const item of MENU[mode]) {
        expect(priceFor(item, 'standard')).toBe(item.price);
        expect(costFor(item, 'standard')).toBe(item.cost);
        expect(priceFor(item, 'cinema')).toBe(p90(item.price * CINEMA));
        expect(costFor(item, 'cinema')).toBe(+(item.cost * CINEMA).toFixed(2));
      }
    }
    expect(priceFor(lookup('film', 10)!, 'cinema')).toBe(14.9);
    expect(priceFor(lookup('film', 20)!, 'cinema')).toBe(26.9);
    expect(costFor(lookup('film', 20)!, 'cinema')).toBe(12.45);
    expect(lookup('film', 15)).toBeNull();
  });
});

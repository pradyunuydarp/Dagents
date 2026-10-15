/**
 * A small fuzzy matcher for the site's searches.
 *
 * Each item is split into words. A query word matches a word of the item when
 * it is the same word, the start of it, part of it, or the same word with a
 * typo or two. So `federat`, `fedration` and `ManifestServ` all find what they
 * mean. Every query word must match, and the score adds up how well each one
 * matched and how much the field it matched in counts.
 *
 * All words of all items go into one vocabulary, so a query compares each of
 * its words with each distinct word once, not with every item.
 */

export interface Field {
  text: string;
  /** How much a match in this field counts: 1 for a title, less for body text. */
  weight: number;
}

export interface Match<T> {
  item: T;
  score: number;
  /** The item's words that matched, for highlighting. */
  words: Set<string>;
}

/**
 * The words of a text, lower case. A camel-case name gives its parts and the
 * whole: `ManifestService` gives `manifest`, `service` and `manifestservice`.
 */
export function words(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.match(/[\p{L}\p{N}]+/gu) ?? []) {
    const lower = raw.toLowerCase();
    out.push(lower);
    const parts = raw.match(/\p{Lu}+(?!\p{Ll})|\p{Lu}?\p{Ll}+|\p{N}+/gu);
    if (parts && parts.length > 1) for (const part of parts) out.push(part.toLowerCase());
  }
  return out;
}

/** The number of typos a query word of this length may have. */
function typosAllowed(length: number): number {
  return length <= 3 ? 0 : length <= 6 ? 1 : 2;
}

/**
 * Edit distance with swapped neighbours counted as one edit, stopping early
 * once it is certain to exceed `max`.
 */
function distance(a: string, b: string, max: number): number {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let before = new Array<number>(b.length + 1).fill(0);
  let previous = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const current = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      let value = Math.min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) value = Math.min(value, before[j - 2] + 1);
      current.push(value);
      best = Math.min(best, value);
    }
    if (best > max) return max + 1;
    before = previous;
    previous = current;
  }
  return previous[b.length];
}

/** How well a query word matches one word of an item, from 0 (not at all) to 1. */
export function wordScore(query: string, word: string): number {
  if (word === query) return 1;
  if (word.startsWith(query)) return 0.85;
  if (query.length >= 3 && word.includes(query)) return 0.6;
  const max = typosAllowed(query.length);
  if (!max) return 0;
  const whole = distance(query, word, max);
  if (whole <= max) return 0.7 - 0.15 * whole;
  // A partly typed word with a typo: compare with the start of the word.
  if (word.length > query.length) {
    const start = distance(query, word.slice(0, query.length), max);
    if (start <= max) return 0.55 - 0.15 * start;
  }
  return 0;
}

export class FuzzyIndex<T> {
  private readonly items: T[];
  private readonly labels: string[];
  /** Each distinct word, and for each item that has it, the highest field weight it has there. */
  private readonly vocabulary = new Map<string, Map<number, number>>();

  /** `fields` lists an item's fields; the first one is its label. */
  constructor(items: T[], fields: (item: T) => Field[]) {
    this.items = items;
    this.labels = items.map((item, id) => {
      const list = fields(item);
      for (const field of list) {
        for (const word of words(field.text)) {
          let postings = this.vocabulary.get(word);
          if (!postings) this.vocabulary.set(word, (postings = new Map()));
          postings.set(id, Math.max(postings.get(id) ?? 0, field.weight));
        }
      }
      return (list[0]?.text ?? "").toLowerCase();
    });
  }

  search(query: string, limit = 30): Match<T>[] {
    const terms = [...new Set(words(query))];
    if (!terms.length) return [];
    let scores: Map<number, number> | null = null;
    const matched = new Map<number, Set<string>>();

    for (const term of terms) {
      const termScores = new Map<number, number>();
      for (const [word, postings] of this.vocabulary) {
        const score = wordScore(term, word);
        if (!score) continue;
        for (const [id, weight] of postings) {
          if (scores && !scores.has(id)) continue;
          if (score * weight > (termScores.get(id) ?? 0)) termScores.set(id, score * weight);
          let set = matched.get(id);
          if (!set) matched.set(id, (set = new Set()));
          set.add(word);
        }
      }
      // Every query word must match: keep the items that matched this one too.
      const next = new Map<number, number>();
      for (const [id, score] of termScores) next.set(id, (scores?.get(id) ?? 0) + score);
      scores = next;
      if (!scores.size) return [];
    }

    // A label that holds the query as typed ranks first.
    const phrase = query.trim().toLowerCase();
    return [...(scores ?? [])]
      .map(([id, score]) => {
        const label = this.labels[id];
        const bonus = label === phrase ? 3 : label.startsWith(phrase) ? 2 : label.includes(phrase) ? 1 : 0;
        return { item: this.items[id], score: score + bonus, words: matched.get(id) ?? new Set<string>() };
      })
      .sort((a, b) => b.score - a.score)
      .slice(0, limit);
  }
}

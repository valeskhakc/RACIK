// icons.js
// Generic, slot-keyed illustrations used in place of the old mockup's
// per-dish-name art library. The backend has no per-dish illustration
// assets, so every dish in a given tray slot (staple/hewani/nabati/sayur/
// buah) shares one icon for that slot — a disclosed simplification, not a
// missing feature. Style matches the mockup's original hand-drawn SVGs:
// flat linework, warm palette, no external assets.

export const slotIcons = {
  staple: `<svg viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
    <ellipse cx="50" cy="60" rx="28" ry="16" fill="#FDFBF4"/>
    <ellipse cx="50" cy="55" rx="26" ry="14" fill="#FFFFFF"/>
    <path d="M34 52 Q50 44 66 52" stroke="#EFE9D8" stroke-width="2" fill="none" stroke-linecap="round"/>
    <path d="M38 58 Q50 52 62 58" stroke="#EFE9D8" stroke-width="2" fill="none" stroke-linecap="round"/>
  </svg>`,
  hewani: `<svg viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
    <ellipse cx="50" cy="78" rx="34" ry="6" fill="#1F2B22" opacity="0.08"/>
    <path d="M32 55 C28 40 38 24 52 24 C66 24 74 38 70 54 C68 62 62 70 55 74 L45 74 C38 70 34 63 32 55 Z" fill="#C0392B"/>
    <path d="M40 40 C42 34 48 30 54 31 C60 32 64 38 62 45" stroke="#7A2018" stroke-width="2.5" stroke-linecap="round"/>
    <circle cx="46" cy="42" r="2" fill="#1F2B22"/>
  </svg>`,
  nabati: `<svg viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
    <ellipse cx="50" cy="80" rx="34" ry="6" fill="#1F2B22" opacity="0.08"/>
    <rect x="30" y="42" width="16" height="16" rx="2" fill="#F3EEDD" stroke="#D8CFA8" stroke-width="1.5"/>
    <rect x="50" y="48" width="16" height="16" rx="2" fill="#F3EEDD" stroke="#D8CFA8" stroke-width="1.5"/>
    <rect x="40" y="60" width="16" height="16" rx="2" fill="#F3EEDD" stroke="#D8CFA8" stroke-width="1.5"/>
  </svg>`,
  sayur: `<svg viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
    <ellipse cx="50" cy="80" rx="30" ry="5" fill="#1F2B22" opacity="0.08"/>
    <path d="M30 60 Q26 44 36 32 Q40 44 38 58 Z" fill="#3F6B4F"/>
    <path d="M46 62 Q40 42 52 26 Q58 42 54 60 Z" fill="#4F7D5C"/>
    <path d="M62 60 Q60 44 70 34 Q74 46 68 58 Z" fill="#3F6B4F"/>
    <circle cx="42" cy="66" r="4" fill="#E8A33D"/>
  </svg>`,
  buah: `<svg viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
    <ellipse cx="50" cy="80" rx="30" ry="5" fill="#1F2B22" opacity="0.08"/>
    <circle cx="42" cy="52" r="20" fill="#C0392B"/>
    <circle cx="62" cy="56" r="14" fill="#E8A33D"/>
    <path d="M42 32 Q46 24 54 26" stroke="#3F6B4F" stroke-width="3" stroke-linecap="round" fill="none"/>
  </svg>`,
};

export const slotLabels = {
  staple: 'Staple',
  hewani: 'Main dish',
  nabati: 'Plant protein',
  sayur: 'Vegetable',
  buah: 'Fruit',
};

/** Educational analogy, not a mechanism or efficacy claim for a G&L product. */
export const microSteps = [
  {
    title: 'Вокруг клетки',
    text: 'Клетка получает сигналы из окружающей среды.',
  },
  {
    title: 'На поверхности',
    text: 'Мембрана отделяет внутреннюю среду. На ней находятся рецепторы.',
  },
  {
    title: 'Встреча с рецептором',
    text: 'Подходящая сигнальная молекула связывается с рецептором.',
  },
  {
    title: 'Сигнал внутри',
    text: 'Рецептор передаёт сигнал внутрь. Самой молекуле не обязательно входить в клетку.',
  },
  {
    title: 'Клеточный ответ',
    text: 'Внутриклеточные участники передают сигнал дальше. Ответ зависит от клетки и сигнального пути.',
  },
] as const;
export function microFrame(progress: number) {
  const p = Math.max(0, Math.min(1, Number.isFinite(progress) ? progress : 0));
  const approach = Math.min(1, Math.max(0, (p - 0.2) / 0.35));
  return {
    step: Math.min(4, Math.floor(p * 5)),
    ligandX: approach === 1 ? 0 : -190 * (1 - approach),
    ligandY: approach === 1 ? 0 : -105 * (1 - approach),
    signal: Math.min(1, Math.max(0, (p - 0.55) / 0.3)),
    scale: 0.88 + Math.sin(p * Math.PI) * 0.12,
  };
}

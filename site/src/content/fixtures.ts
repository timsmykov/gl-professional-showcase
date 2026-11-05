import type { ContentImage, PublicSiteContent } from './site.types';
import { productBrandCopy } from './product-brand-copy';

const packagingImages: Record<string, ContentImage> = {
  cute: {
    src: '/images/products/cute/photo-alpha-20261009-1000.webp',
    srcSet:
      '/images/products/cute/photo-alpha-20261009-400.webp 400w, /images/products/cute/photo-alpha-20261009-1000.webp 1000w',
    alt: 'Флакон и коробка CUTE',
    width: 1000,
    height: 1260,
  },
  'plla-activator': {
    src: '/images/products/plla-activator/photo-alpha-20261009-1000.webp',
    srcSet:
      '/images/products/plla-activator/photo-alpha-20261009-400.webp 400w, /images/products/plla-activator/photo-alpha-20261009-1000.webp 1000w',
    alt: 'Флаконы и коробка PLLA + Activator',
    width: 1000,
    height: 882,
  },
  'cute-ultra': {
    src: '/images/products/cute-ultra/photo-alpha-20261009-1000.webp',
    srcSet:
      '/images/products/cute-ultra/photo-alpha-20261009-400.webp 400w, /images/products/cute-ultra/photo-alpha-20261009-1000.webp 1000w',
    alt: 'Флакон и коробка CUTE Ultra',
    width: 1000,
    height: 1407,
  },
  'nad-skin-matrix': {
    src: '/images/products/nad-skin-matrix/photo-alpha-20261009-1000.webp',
    srcSet:
      '/images/products/nad-skin-matrix/photo-alpha-20261009-400.webp 400w, /images/products/nad-skin-matrix/photo-alpha-20261009-1000.webp 1000w',
    alt: 'Флакон и коробка NAD+ Skin Matrix',
    width: 1000,
    height: 631,
  },
};

const visual = (product: string, file: string, alt: string): ContentImage => ({
  src: `/images/products/${product}/${file}-quality-20261011.webp`,
  srcSet: `/images/products/${product}/${file}-quality-20261011-1024.webp 1024w, /images/products/${product}/${file}-quality-20261011.webp 2048w`,
  alt,
  width: 2048,
  height: 2560,
});

const clinical = (
  product: string,
  file: string,
  alt: string,
  width = 1600,
  height = 1600,
): ContentImage => {
  const caseNumber = file.match(/^clinical-(\d+)/)?.[1];
  const combinedLabel = file.includes('cute-nad')
    ? 'Сочетание CUTE и NAD+'
    : 'Сочетание CUTE и PLLA+';
  return {
    src: `/images/products/${product}/${file}.webp`,
    alt,
    width,
    height,
    caseKey: product === 'combined' ? file : `${product}-${caseNumber}`,
    caseLabel:
      product === 'combined' ? combinedLabel : `Случай ${Number(caseNumber)}`,
  };
};

const nadVisuals = [
  visual(
    'nad-skin-matrix',
    'visual-01',
    'Визуальная концепция NAD+ Skin Matrix',
  ),
  visual(
    'nad-skin-matrix',
    'visual-02',
    'Информационный визуал NAD+ Skin Matrix',
  ),
  visual(
    'nad-skin-matrix',
    'visual-03',
    'Вертикальный визуал NAD+ Skin Matrix',
  ),
];

const pllaVisuals = [
  visual('plla-activator', 'visual-01', 'Визуальная концепция PLLA+ Activator'),
  visual(
    'plla-activator',
    'visual-02',
    'Информационный визуал PLLA+ Activator',
  ),
  visual('plla-activator', 'visual-03', 'Вертикальный визуал PLLA+ Activator'),
];

const cuteVisuals = [
  visual('cute', 'visual-01', 'Визуальная концепция CUTE'),
  visual('cute', 'visual-02', 'Информационный визуал CUTE'),
];

const cuteUltraVisuals = [
  visual('cute-ultra', 'visual-01', 'Визуальная концепция CUTE Ultra'),
];

const nadClinical = [
  clinical(
    'nad-skin-matrix',
    'clinical-01-view-01',
    'Клиническое изображение NAD+ Skin Matrix, кейс 1, ракурс 1',
  ),
  clinical(
    'nad-skin-matrix',
    'clinical-01-view-02',
    'Клиническое изображение NAD+ Skin Matrix, кейс 1, ракурс 2',
  ),
  clinical(
    'combined',
    'cute-nad-clinical-01',
    'Клиническое изображение комбинированного кейса CUTE и NAD+',
    1280,
    1280,
  ),
];

const pllaClinical = [
  clinical(
    'plla-activator',
    'clinical-01-view-01',
    'Клиническое изображение PLLA+ Activator, кейс 1, ракурс 1',
  ),
  clinical(
    'plla-activator',
    'clinical-01-view-02',
    'Клиническое изображение PLLA+ Activator, кейс 1, ракурс 2',
  ),
  clinical(
    'plla-activator',
    'clinical-01-view-03',
    'Клиническое изображение PLLA+ Activator, кейс 1, ракурс 3',
  ),
  clinical(
    'plla-activator',
    'clinical-02-face',
    'Клиническое изображение PLLA+ Activator, кейс 2',
  ),
  clinical(
    'plla-activator',
    'clinical-03-face',
    'Клиническое изображение PLLA+ Activator, кейс 3',
  ),
  clinical(
    'plla-activator',
    'clinical-04-face',
    'Клиническое изображение PLLA+ Activator, кейс 4',
  ),
  clinical(
    'plla-activator',
    'clinical-05-face',
    'Клиническое изображение PLLA+ Activator, кейс 5',
    720,
    1280,
  ),
  clinical(
    'plla-activator',
    'clinical-06-face',
    'Клиническое изображение PLLA+ Activator, кейс 6',
    1280,
    1280,
  ),
  clinical(
    'plla-activator',
    'clinical-07-view-01',
    'Клиническое изображение PLLA+ Activator, кейс 7, ракурс 1',
    1280,
    1280,
  ),
  clinical(
    'plla-activator',
    'clinical-07-view-02',
    'Клиническое изображение PLLA+ Activator, кейс 7, ракурс 2',
    1280,
    1280,
  ),
  clinical(
    'plla-activator',
    'clinical-07-view-03',
    'Клиническое изображение PLLA+ Activator, кейс 7, ракурс 3',
    1280,
    1280,
  ),
  clinical(
    'plla-activator',
    'clinical-08-face',
    'Клиническое изображение PLLA+ Activator, кейс 8',
    968,
    1280,
  ),
  clinical(
    'plla-activator',
    'clinical-09-face',
    'Клиническое изображение PLLA+ Activator, кейс 9',
    968,
    1280,
  ),
  clinical(
    'plla-activator',
    'clinical-10-face',
    'Клиническое изображение PLLA+ Activator, кейс 10',
    1280,
    1280,
  ),
  clinical(
    'combined',
    'cute-plla-clinical-02',
    'Клиническое изображение комбинированного кейса CUTE и PLLA+',
    1280,
    1280,
  ),
];

const cuteClinical = [
  clinical(
    'cute',
    'clinical-03-lower-face',
    'Клиническое изображение CUTE, кейс 3',
  ),
  clinical(
    'cute',
    'clinical-04-midface',
    'Клиническое изображение CUTE, кейс 4',
  ),
  clinical(
    'cute',
    'clinical-05-parotid',
    'Клиническое изображение CUTE, кейс 5',
  ),
  clinical(
    'cute',
    'clinical-06-face',
    'Клиническое изображение CUTE, кейс 6',
    1280,
    1280,
  ),
  clinical(
    'cute',
    'clinical-07-periorbital',
    'Клиническое изображение CUTE, кейс 7',
  ),
  clinical(
    'combined',
    'cute-nad-clinical-01',
    'Клиническое изображение комбинированного кейса CUTE и NAD+',
    1280,
    1280,
  ),
  clinical(
    'combined',
    'cute-plla-clinical-02',
    'Клиническое изображение комбинированного кейса CUTE и PLLA+',
    1280,
    1280,
  ),
];

export const fixtureContent: PublicSiteContent = {
  products: [
    {
      slug: 'nad-skin-matrix',
      title: 'NAD+ Skin Matrix',
      line: 'nad',
      lineLabel: 'NAD+',
      summary:
        'Гель на основе гиалуроновой кислоты и комплекса NAD+ объёмом 6 мл.',
      description:
        'NAD+ Skin Matrix — прозрачный гель с двумя формами гиалуроната натрия и комплексом NAD+. На странице собраны состав по материалу бренда и фотографии отдельных случаев применения.',
      facts: [
        { label: 'Форма и объём', value: 'Прозрачный гель, 6 мл' },
        {
          label: 'Ключевые компоненты',
          value:
            'Гиалуронат натрия двух концентраций, комплекс NAD+, сукцинат натрия, трегалоза, центелла азиатская',
        },
      ],
      distinguishingFact: 'Две формы гиалуроната натрия и комплекс NAD+.',
      contentNote:
        'Состав и форма взяты из клиентского документа от 21.09. Правовая категория, инструкция и регистрационные документы требуют отдельной сверки.',
      brandCopy: productBrandCopy['nad-skin-matrix'],
      cover: nadVisuals[0]!,
      packagingPhoto: {
        hero: packagingImages['nad-skin-matrix']!,
        card: packagingImages['nad-skin-matrix']!,
      },
      visuals: nadVisuals,
      clinicalImages: nadClinical,
      status: 'published',
    },
    {
      slug: 'plla-activator',
      title: 'PLLA+ Activator',
      line: 'plla',
      lineLabel: 'PLLA+',
      summary:
        'Двухкомпонентный продукт: флакон с 150 мг поли-L-молочной кислоты и активатор 5 мл.',
      description:
        'PLLA+ Activator состоит из двух флаконов: сухого вещества поли-L-молочной кислоты и прозрачного раствора-активатора. На странице представлены состав по материалу бренда и фотографии отдельных случаев.',
      facts: [
        {
          label: 'Форма',
          value: 'Два флакона: сухое вещество и раствор-активатор',
        },
        { label: 'Количество', value: '150 мг PLLA; активатор 5 мл' },
        {
          label: 'Ключевые компоненты активатора',
          value: 'Гиалуронат натрия, PDRN, гексапептид-8, маннитол',
        },
      ],
      distinguishingFact: 'PLLA и активатор размещены в отдельных флаконах.',
      contentNote:
        'Состав и форма взяты из клиентского документа от 21.09. Правовая категория, инструкция и регистрационные документы требуют отдельной сверки.',
      brandCopy: productBrandCopy['plla-activator'],
      cover: pllaVisuals[0]!,
      packagingPhoto: {
        hero: packagingImages['plla-activator']!,
        card: packagingImages['plla-activator']!,
      },
      visuals: pllaVisuals,
      clinicalImages: pllaClinical,
      status: 'published',
    },
    {
      slug: 'cute',
      title: 'CUTE',
      line: 'cute',
      lineLabel: 'CUTE',
      summary:
        'Гелеобразный раствор с рекомбинантным коллагеном 0,1% и PDRN 0,2%, объём 6 мл.',
      description:
        'CUTE — прозрачный гелеобразный раствор на основе рекомбинантного коллагена и PDRN. В составе также указаны идебенон, микроэлементы и другие компоненты.',
      facts: [
        { label: 'Форма и объём', value: 'Гелеобразный раствор, 6 мл' },
        {
          label: 'Ключевые компоненты',
          value:
            'Рекомбинантный коллаген 0,1%, PDRN 0,2%, идебенон, глицинат цинка, хелат селена, трегалоза',
        },
      ],
      distinguishingFact:
        'Рекомбинантный коллаген 0,1%; гиалуроновая кислота в составе не указана.',
      contentNote:
        'Состав и форма взяты из клиентского документа от 21.09. Правовая категория, инструкция и регистрационные документы требуют отдельной сверки.',
      brandCopy: productBrandCopy.cute,
      cover: cuteVisuals[0]!,
      packagingPhoto: {
        hero: packagingImages['cute']!,
        card: packagingImages['cute']!,
      },
      visuals: cuteVisuals,
      clinicalImages: cuteClinical,
      status: 'published',
    },
    {
      slug: 'cute-ultra',
      title: 'CUTE Ultra',
      line: 'cute',
      lineLabel: 'CUTE',
      summary:
        'Гелеобразный раствор с рекомбинантным коллагеном 0,3%, PDRN 0,2% и комплексом аминокислот, объём 6 мл.',
      description:
        'CUTE Ultra — отдельная формула семейства CUTE на основе рекомбинантного коллагена, PDRN и комплекса аминокислот. По материалу бренда концентрация коллагена в Ultra составляет 0,3%, в CUTE — 0,1%.',
      facts: [
        { label: 'Форма и объём', value: 'Гелеобразный раствор, 6 мл' },
        {
          label: 'Ключевые компоненты',
          value:
            'Рекомбинантный коллаген 0,3%, PDRN 0,2%, L-карнитин, маннитол и комплекс аминокислот',
        },
      ],
      distinguishingFact:
        'Коллаген 0,3% против 0,1% у CUTE; добавлен комплекс аминокислот.',
      contentNote:
        'Состав и форма взяты из клиентского документа от 21.09. Правовая категория, инструкция, регистрационные и отдельные клинические материалы не получены.',
      brandCopy: productBrandCopy['cute-ultra'],
      cover: cuteUltraVisuals[0]!,
      packagingPhoto: {
        hero: packagingImages['cute-ultra']!,
        card: packagingImages['cute-ultra']!,
      },
      visuals: cuteUltraVisuals,
      clinicalImages: [],
      status: 'published',
    },
  ],
  webinars: [
    {
      slug: 'demo-case-review',
      publicCode: 'demo_case_review',
      demo: true,
      title: 'Разбор профессионального случая',
      format: 'recording',
      formatLabel: 'Запись',
      summary: 'Разбор профессионального случая и вопросы для обсуждения.',
      program: [
        'Вводная часть',
        'Разбор условного примера',
        'Вопросы для обсуждения',
      ],
      audience: 'Для специалистов косметологии.',
      status: 'published',
    },
    {
      slug: 'demo-questions',
      publicCode: 'demo_questions',
      demo: true,
      title: 'Вопросы и ответы для специалистов',
      format: 'recording',
      formatLabel: 'Запись',
      summary: 'Встреча, посвящённая вопросам специалистов.',
      audience: 'Для специалистов косметологии.',
      status: 'published',
    },
    {
      slug: 'demo-live-meeting',
      demo: true,
      title: 'Открытая встреча',
      format: 'live',
      formatLabel: 'Живая встреча',
      summary: 'Открытая встреча с командой G&L.',
      status: 'published',
    },
  ],
};

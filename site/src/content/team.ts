// Existing client biographies and selected published portraits; no new claims.
export interface TeamMember {
  id: string;
  name: string;
  role: string;
  image: string;
  imageSrcSet: string;
  imageAlt: string;
  imageWidth: number;
  imageHeight: number;
  credentials: string[];
  biography: string[];
  links: { href: string; label: string }[];
}

export const teamMembers: TeamMember[] = [
  {
    id: 'german-cherkasov',
    name: 'Герман Черкасов',
    role: 'Сооснователь G&L',
    image: '/images/team/german-cream-background-20261008-900.webp',
    imageSrcSet:
      '/images/team/german-cream-background-20261008-540.webp 540w, /images/team/german-cream-background-20261008-900.webp 900w',
    imageAlt: 'Герман Черкасов, сооснователь G&L',
    imageWidth: 900,
    imageHeight: 1125,
    credentials: [
      'Кандидат медицинских наук',
      'Врач-хирург, дерматовенеролог, косметолог и организатор здравоохранения',
      'Старший научный сотрудник и врач-трансфузиолог РНЦХ им. акад. Б. В. Петровского',
      'Бизнес-тренер и тренер по инъекционным методам в косметологии',
      'Член Московского общества дерматовенерологов, спикер научных конференций',
    ],
    biography: [
      'Хирургический опыт стал основой его точной и деликатной работы в инъекционной косметологии. Отдельное направление практики — помощь косметологам и пациентам при осложнениях.',
      'Герман восемь лет работает как тренер. В обучении он делает акцент на поддержке коллег и сопровождении их профессионального пути.',
    ],
    links: [
      {
        href: 'https://t.me/dvig_cosmetologa',
        label: 'Канал Германа в Telegram',
      },
      {
        href: 'https://www.instagram.com/drgermancherkasov?stkn=bDhleGg4OWtldzM0',
        label: 'Профиль Германа в Instagram',
      },
    ],
  },
  {
    id: 'lyudmila-chesnokova',
    name: 'Людмила Чеснокова',
    role: 'Сооснователь G&L Cosmetology',
    image: '/images/team/lyudmila-cream-background-20261008-1000.webp',
    imageSrcSet:
      '/images/team/lyudmila-cream-background-20261008-540.webp 540w, /images/team/lyudmila-cream-background-20261008-1000.webp 1000w',
    imageAlt: 'Людмила Чеснокова, сооснователь G&L Cosmetology',
    imageWidth: 1000,
    imageHeight: 1250,
    credentials: [
      'Врач-дерматовенеролог и косметолог',
      'Тренер по инъекционным методам в косметологии',
      'Почти 30 лет профессиональной практики',
      'Обучила более 5000 косметологов по всей России',
      'Автор техник и методик в инъекционной косметологии',
    ],
    biography: [
      'Профессиональный путь Людмилы связан со становлением косметологии в России как практики и научного направления.',
      'Опыт работы с инъекционными препаратами и обучения специалистов стал основой её участия в создании собственной линейки G&L.',
    ],
    links: [
      {
        href: 'https://t.me/honest_cosmetology_chestnokova',
        label: 'Канал Людмилы в Telegram',
      },
    ],
  },
];

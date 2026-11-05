export const telegramBotUrl = 'https://t.me/glprofessional_bot';
export const germanTelegramUrl = 'https://t.me/German_Cherkasov';
export const germanWhatsAppUrl = 'https://wa.me/79153494010';

export function germanWhatsAppProductUrl(productTitle: string): string {
  const url = new URL(germanWhatsAppUrl);
  url.searchParams.set(
    'text',
    `Здравствуйте, Герман! У меня вопрос о препарате ${productTitle}.`,
  );
  return url.toString();
}

export const brandTelegramUrl = 'https://t.me/glcosmetology';
export const brandInstagramUrl =
  'https://www.instagram.com/gl.cosmetology?stkn=ejcxMWcyc2tsc2pk';

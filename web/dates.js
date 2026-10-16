export function formatTimestamp(value, timeZone, timeOnly = false) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const options = {
    timeZone, hourCycle: 'h23',
    hour: '2-digit', minute: '2-digit',
    ...(timeOnly ? { second: '2-digit' } : { year: 'numeric', month: '2-digit', day: '2-digit' }),
  };
  return new Intl.DateTimeFormat('sv-SE', options).format(date);
}


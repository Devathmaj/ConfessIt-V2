import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function resolveProfilePictureUrl(value?: string | null) {
  if (!value) {
    return '/placeholder.svg'
  }

  if (/^https?:\/\//i.test(value)) {
    return value
  }

  if (value.startsWith('data:')) {
    return value
  }

  if (value.startsWith('/')) {
    return value
  }

  return '/placeholder.svg'
}

export function formatDateDDMMYYYY(value: string | null | undefined): string {
  if (!value) return 'Unknown';
  const date = new Date(value);
  if (isNaN(date.getTime())) return 'Invalid date';

  const day = String(date.getDate()).padStart(2, '0');
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const year = date.getFullYear();

  return `${day}/${month}/${year}`;
}

export function formatDateTimeDDMMYYYY(value: string | null | undefined): string {
  if (!value) return 'Unknown';
  const date = new Date(value);
  if (isNaN(date.getTime())) return 'Invalid date';

  const day = String(date.getDate()).padStart(2, '0');
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const year = date.getFullYear();
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');

  return `${day}/${month}/${year} ${hours}:${minutes}`;
}

import { toast as sonner } from "sonner";

export function toastOk(msg: string, sub?: string) {
  sonner.success(msg, sub ? { description: sub } : undefined);
}
export function toastErr(msg: string, sub?: string) {
  sonner.error(msg, sub ? { description: sub } : undefined);
}

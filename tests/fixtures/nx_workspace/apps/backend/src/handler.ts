import { ProductService } from '@org/marketplace';

export function handler() {
  const svc = new ProductService();
  return svc.run();
}

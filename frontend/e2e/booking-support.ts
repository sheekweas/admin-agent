import { expect, type APIRequestContext } from '@playwright/test';

export const bookingUnique = () => `${Date.now()}${Math.floor(Math.random() * 100000)}`;
export const bookingPhone = () => `+7${bookingUnique().slice(-10)}`;
export async function bookingJson<T>(api: APIRequestContext, method: string, path: string, data?: unknown): Promise<T> {
  const response = await api.fetch(path, { method, data });
  expect(response.ok(), `${method} ${path}: ${response.status()} ${await response.text()}`).toBeTruthy();
  return response.status() === 204 ? undefined as T : await response.json() as T;
}
export interface BookingSlot { slot_id: string; staff_id: string; branch_id: string; start_at: string; staff_name: string }
export interface TestBooking { id: string; status: string; contact: { id: string; name: string }; total_price: string }

/** Each test owns a service and member so parallel workers never compete for demo slots. */
export async function provisionBookingResources(api: APIRequestContext) {
  const suffix = bookingUnique();
  const { items: branches } = await bookingJson<{items: {id: string; timezone: string}[]}>(api, 'GET', '/v1/branches');
  const branch = branches[0];
  expect(branch, 'Seed must provide at least one branch').toBeTruthy();
  const service = await bookingJson<{id: string; title_ru: string}>(api, 'POST', '/v1/services', {
    title_ru: `E2E service ${suffix}`, title_kk: `E2E service ${suffix}`, duration_min: 30, price: '1234.00', published: true,
  });
  const staff = await bookingJson<{id: string; name: string}>(api, 'POST', '/v1/staff', {
    name: `E2E barber ${suffix}`, branch_id: branch.id, service_ids: [service.id],
  });
  await bookingJson(api, 'PUT', `/v1/staff/${staff.id}/shifts`, Array.from({length: 7}, (_, weekday) => ({weekday, start_time: '09:00', end_time: '21:00', breaks: []})));
  const day = new Intl.DateTimeFormat('en-CA', {timeZone: branch.timezone, year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date(Date.now() + 86400000));
  const {items: slots} = await bookingJson<{items: BookingSlot[]}>(api, 'GET', `/v1/availability?${new URLSearchParams({service_id: service.id, staff_id: staff.id, branch_id: branch.id, date_from: day, date_to: day})}`);
  expect(slots.length, 'Own staff schedule must offer slots tomorrow').toBeGreaterThan(1);
  return {branch, service, staff, day, slots};
}

export async function cleanupBookingResources(api: APIRequestContext, resources: Awaited<ReturnType<typeof provisionBookingResources>>, bookings: TestBooking[], paymentIds: string[] = []) {
  for (const id of paymentIds) await bookingJson(api, 'POST', `/v1/payments/${id}/cancel`, {reason: 'E2E cleanup'});
  for (const booking of bookings) {
    const current = await bookingJson<TestBooking>(api, 'GET', `/v1/bookings/${booking.id}`);
    if (['held', 'pending', 'confirmed'].includes(current.status)) await bookingJson(api, 'PATCH', `/v1/bookings/${booking.id}`, {action: 'cancel', reason: 'E2E cleanup'});
  }
  await bookingJson(api, 'PUT', `/v1/staff/${resources.staff.id}/shifts`, []);
  await bookingJson(api, 'PATCH', `/v1/staff/${resources.staff.id}`, {active: false});
  await bookingJson(api, 'DELETE', `/v1/services/${resources.service.id}`);
}

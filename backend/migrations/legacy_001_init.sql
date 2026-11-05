BEGIN;

CREATE TABLE IF NOT EXISTS users (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  telegram_id BIGINT NOT NULL UNIQUE,
  last_section TEXT CHECK (last_section IN ('client', 'pro')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_sections (
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  section TEXT NOT NULL CHECK (section IN ('client', 'pro')),
  first_opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, section)
);

CREATE TABLE IF NOT EXISTS services (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  active BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS appointment_slots (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  service_id BIGINT NOT NULL REFERENCES services(id),
  starts_at TIMESTAMPTZ NOT NULL,
  ends_at TIMESTAMPTZ NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  CHECK (ends_at > starts_at)
);
CREATE INDEX IF NOT EXISTS appointment_slots_open_idx ON appointment_slots(service_id, starts_at) WHERE active;

CREATE TABLE IF NOT EXISTS booking_requests (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id),
  slot_id BIGINT NOT NULL REFERENCES appointment_slots(id),
  source_update_id BIGINT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'declined')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at TIMESTAMPTZ,
  client_notified_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS booking_requests_live_slot_idx ON booking_requests(slot_id) WHERE status IN ('pending', 'confirmed');
CREATE INDEX IF NOT EXISTS booking_requests_user_idx ON booking_requests(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS products (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  active BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS webinars (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  price_kopeks INTEGER CHECK (price_kopeks >= 0),
  active BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS webinar_entitlements (
  user_id BIGINT NOT NULL REFERENCES users(id),
  webinar_id BIGINT NOT NULL REFERENCES webinars(id),
  source TEXT NOT NULL CHECK (source IN ('paid', 'imported')),
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, webinar_id)
);

CREATE TABLE IF NOT EXISTS processed_updates (
  update_id BIGINT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('processing', 'done')),
  claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ
);

COMMIT;

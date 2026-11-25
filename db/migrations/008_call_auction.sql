-- 008: fills from the opening / closing call auction (集合竞价).
ALTER TYPE fill_liquidity ADD VALUE IF NOT EXISTS 'auction';

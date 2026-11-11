"""The 1-minute gold rule's predictions, their outcomes, and the owner's approvals in advance
(S85, the owner's directive of 2026-11-11).

- ``gold_predictions``: one row per 1-minute candle and model: the probability that a buy, and
  that a short, reaches its take-profit before its stop-loss, or why there is no reliable
  estimate. Append-only. Recorded whether or not a robot runs: this is the forward test.
- ``gold_prediction_outcomes``: what HFM's prices then did for each direction (TP first, SL
  first, or neither within four hours). Append-only. No trade is placed to learn it.
- ``robot_tickets``: the owner's approval of the robot's next trade, given in advance with
  their authenticator code. A 1-minute trade is over in about two minutes (lab Run 7), so an
  approval asked after the signal would come too late. One waiting per robot; it ends USED (by
  one trade), REFUSED (the checks refused that trade), CANCELLED (by the owner) or EXPIRED, and
  only its ending is ever written.

The downgrade refuses while any of these holds a row, or a ``gold_m1`` mandate exists.

Revision ID: 0031_gold_predictions
Revises: 0030_broker_connections
Create Date: 2026-11-11
"""

from cosmic.db.sqlsplit import run_script

revision = "0031_gold_predictions"
down_revision = "0030_broker_connections"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    app = '"' + app_role.replace('"', '""') + '"'
    run_script(
        f"""
    CREATE TABLE gold_predictions (
      id uuid PRIMARY KEY,
      bar timestamptz NOT NULL,
      model text NOT NULL,
      at timestamptz NOT NULL,
      bid numeric(38,18),
      ask numeric(38,18),
      p_buy numeric(6,5) CHECK (p_buy BETWEEN 0 AND 1),
      p_sell numeric(6,5) CHECK (p_sell BETWEEN 0 AND 1),
      setup text NOT NULL CHECK (setup IN ('BUY','SELL','WAIT')),
      why text,
      features jsonb NOT NULL,
      CONSTRAINT gold_predictions_bar_model UNIQUE (bar, model),
      CHECK ((p_buy IS NULL) = (p_sell IS NULL)),
      CHECK (p_buy IS NOT NULL OR why IS NOT NULL),
      CHECK (p_buy IS NOT NULL OR setup = 'WAIT')
    );
    CREATE TRIGGER gold_predictions_immutable BEFORE UPDATE OR DELETE ON gold_predictions
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON gold_predictions FROM {app};

    CREATE TABLE gold_prediction_outcomes (
      prediction_id uuid NOT NULL REFERENCES gold_predictions (id),
      side text NOT NULL CHECK (side IN ('BUY','SELL')),
      outcome text NOT NULL CHECK (outcome IN ('TP','SL','NONE')),
      resolved_at timestamptz NOT NULL,
      minutes integer NOT NULL CHECK (minutes >= 0),
      PRIMARY KEY (prediction_id, side)
    );
    CREATE TRIGGER gold_prediction_outcomes_immutable
      BEFORE UPDATE OR DELETE ON gold_prediction_outcomes
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
    REVOKE UPDATE, DELETE, TRUNCATE ON gold_prediction_outcomes FROM {app};

    CREATE TABLE robot_tickets (
      id uuid PRIMARY KEY,
      mandate_id uuid NOT NULL REFERENCES trading_mandates (id),
      armed_at timestamptz NOT NULL,
      armed_by text NOT NULL,
      expires_at timestamptz NOT NULL,
      shown_probability numeric(6,5) CHECK (shown_probability BETWEEN 0 AND 1),
      practice boolean NOT NULL,
      ended text CHECK (ended IN ('USED','REFUSED','CANCELLED','EXPIRED')),
      ended_at timestamptz,
      intent_id uuid REFERENCES trade_intents (id),
      CHECK (expires_at > armed_at),
      CHECK ((ended IS NULL) = (ended_at IS NULL)),
      CHECK (intent_id IS NULL OR ended IN ('USED','REFUSED')),
      CHECK (ended IS DISTINCT FROM 'USED' OR intent_id IS NOT NULL)
    );
    CREATE UNIQUE INDEX robot_tickets_one_waiting ON robot_tickets (mandate_id)
      WHERE ended IS NULL;
    CREATE FUNCTION robot_tickets_sealed() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.ended IS NOT NULL OR NEW.ended IS NULL
         OR NEW.id <> OLD.id OR NEW.mandate_id <> OLD.mandate_id
         OR NEW.armed_at <> OLD.armed_at OR NEW.armed_by <> OLD.armed_by
         OR NEW.expires_at <> OLD.expires_at OR NEW.practice <> OLD.practice
         OR NEW.shown_probability IS DISTINCT FROM OLD.shown_probability THEN
        RAISE EXCEPTION 'an approval in advance is sealed: only its ending is recorded, once'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER robot_tickets_sealed BEFORE UPDATE ON robot_tickets
      FOR EACH ROW EXECUTE FUNCTION robot_tickets_sealed();
    REVOKE DELETE, TRUNCATE ON robot_tickets FROM {app};
    """
    )


def downgrade(app_role: str = "cosmic_app") -> None:
    run_script(
        """
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM gold_predictions) OR EXISTS (SELECT 1 FROM robot_tickets)
         OR EXISTS (SELECT 1 FROM trading_mandates WHERE strategy_key = 'gold_m1') THEN
        RAISE EXCEPTION 'gold predictions exist; the schema before 0031 cannot hold them';
      END IF;
    END $$;
    DROP TABLE robot_tickets;
    DROP FUNCTION robot_tickets_sealed();
    DROP TABLE gold_prediction_outcomes;
    DROP TABLE gold_predictions;
    """
    )

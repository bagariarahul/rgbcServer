/**
 * Sprint 2 Migration: Add P2P Master-Slave columns to devices table.
 *
 * WHY A MANUAL MIGRATION:
 *   database.js uses sync({ force: false, alter: false }) which never
 *   adds/alters columns. This script runs raw SQL ALTER TABLE statements
 *   with IF NOT EXISTS checks so it's safe to run multiple times.
 *
 * USAGE:
 *   docker exec -it <container_name> node src/migrations/sprint2_devices_p2p.js
 *
 *   Or from the host (if running locally):
 *   node src/migrations/sprint2_devices_p2p.js
 */

require('dotenv').config();
const { Sequelize } = require('sequelize');

const sequelize = new Sequelize(
    process.env.DB_NAME || 'cloudbackup',
    process.env.DB_USER || 'cloudbackup_user',
    process.env.DB_PASSWORD || 'secure_demo_password',
    {
        host: process.env.DB_HOST || 'db',
        port: process.env.DB_PORT || 5432,
        dialect: 'postgres',
        logging: console.log
    }
);

async function migrate() {
    console.log('═══════════════════════════════════════════════════════');
    console.log('  Sprint 2 Migration: P2P Master-Slave Columns');
    console.log('═══════════════════════════════════════════════════════');

    try {
        await sequelize.authenticate();
        console.log('✅ Database connected\n');

        // Each ALTER uses a DO block with a column-existence check
        // so the migration is idempotent (safe to run repeatedly).

        const alterStatements = [
            {
                name: 'role',
                sql: `
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'devices' AND column_name = 'role'
                        ) THEN
                            ALTER TABLE devices ADD COLUMN role VARCHAR(10) NOT NULL DEFAULT 'SLAVE';
                            RAISE NOTICE 'Added column: role';
                        ELSE
                            RAISE NOTICE 'Column already exists: role';
                        END IF;
                    END $$;
                `
            },
            {
                name: 'tunnel_url',
                sql: `
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'devices' AND column_name = 'tunnel_url'
                        ) THEN
                            ALTER TABLE devices ADD COLUMN tunnel_url TEXT DEFAULT NULL;
                            RAISE NOTICE 'Added column: tunnel_url';
                        ELSE
                            RAISE NOTICE 'Column already exists: tunnel_url';
                        END IF;
                    END $$;
                `
            },
            {
                name: 'local_ip',
                sql: `
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'devices' AND column_name = 'local_ip'
                        ) THEN
                            ALTER TABLE devices ADD COLUMN local_ip VARCHAR(45) DEFAULT NULL;
                            RAISE NOTICE 'Added column: local_ip';
                        ELSE
                            RAISE NOTICE 'Column already exists: local_ip';
                        END IF;
                    END $$;
                `
            },
            {
                name: 'local_port',
                sql: `
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'devices' AND column_name = 'local_port'
                        ) THEN
                            ALTER TABLE devices ADD COLUMN local_port INTEGER DEFAULT 8741;
                            RAISE NOTICE 'Added column: local_port';
                        ELSE
                            RAISE NOTICE 'Column already exists: local_port';
                        END IF;
                    END $$;
                `
            },
            {
                name: 'file_count',
                sql: `
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'devices' AND column_name = 'file_count'
                        ) THEN
                            ALTER TABLE devices ADD COLUMN file_count INTEGER DEFAULT 0;
                            RAISE NOTICE 'Added column: file_count';
                        ELSE
                            RAISE NOTICE 'Column already exists: file_count';
                        END IF;
                    END $$;
                `
            },
            {
                name: 'disk_free_bytes',
                sql: `
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'devices' AND column_name = 'disk_free_bytes'
                        ) THEN
                            ALTER TABLE devices ADD COLUMN disk_free_bytes BIGINT DEFAULT 0;
                            RAISE NOTICE 'Added column: disk_free_bytes';
                        ELSE
                            RAISE NOTICE 'Column already exists: disk_free_bytes';
                        END IF;
                    END $$;
                `
            },
            {
                name: 'os_platform',
                sql: `
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'devices' AND column_name = 'os_platform'
                        ) THEN
                            ALTER TABLE devices ADD COLUMN os_platform VARCHAR(20) DEFAULT NULL;
                            RAISE NOTICE 'Added column: os_platform';
                        ELSE
                            RAISE NOTICE 'Column already exists: os_platform';
                        END IF;
                    END $$;
                `
            }
        ];

        // Run each ALTER statement
        for (const stmt of alterStatements) {
            console.log(`Migrating: ${stmt.name}...`);
            await sequelize.query(stmt.sql);
            console.log(`  ✅ ${stmt.name} done`);
        }

        // Add composite index for fast Master lookup
        console.log('\nAdding indexes...');
        try {
            await sequelize.query(`
                CREATE INDEX IF NOT EXISTS idx_devices_role
                    ON devices (role);
            `);
            console.log('  ✅ idx_devices_role');

            await sequelize.query(`
                CREATE INDEX IF NOT EXISTS idx_devices_master_lookup
                    ON devices (role, is_active, last_seen_at DESC);
            `);
            console.log('  ✅ idx_devices_master_lookup');
        } catch (e) {
            console.log(`  ⚠️ Index creation note: ${e.message}`);
        }

        console.log('\n═══════════════════════════════════════════════════════');
        console.log('  ✅ Sprint 2 migration complete!');
        console.log('═══════════════════════════════════════════════════════');

        // Verify columns
        const [results] = await sequelize.query(`
            SELECT column_name, data_type, column_default
            FROM information_schema.columns
            WHERE table_name = 'devices'
            AND column_name IN ('role', 'tunnel_url', 'local_ip', 'local_port', 'file_count', 'disk_free_bytes', 'os_platform')
            ORDER BY column_name;
        `);
        console.log('\nVerification:');
        results.forEach(r => {
            console.log(`  ${r.column_name}: ${r.data_type} (default: ${r.column_default})`);
        });

    } catch (error) {
        console.error('❌ Migration failed:', error);
        process.exit(1);
    } finally {
        await sequelize.close();
    }
}

migrate();
const { Sequelize } = require('sequelize');
const logger = require('./logger');

const sequelize = new Sequelize(
    process.env.DB_NAME || 'cloudbackup',
    process.env.DB_USER || 'cloudbackup_user',
    process.env.DB_PASSWORD || 'secure_demo_password',
    {
        // FIX: Prioritize Docker service name 'db', fallback to localhost
        host: process.env.DB_HOST || 'localhost',
        port: process.env.DB_PORT || 5432,
        dialect: 'postgres',
        logging: msg => logger.debug(msg),
        pool: { max: 10, min: 0, acquire: 30000, idle: 10000 }
    }
);

// ... (Keep your model imports here: User, File, etc.) ...
// RE-IMPORT YOUR MODELS HERE IF NEEDED, e.g.:
// const User = require('../models/User')(sequelize); 
// ...

const connectDB = async () => {
    try {
        await sequelize.authenticate();
        logger.info('✅ Database connection established');

        // ... (Keep your setupAssociations() call here) ...
        // setupAssociations();

        // FIX: Logic to handle the migration crash
        if (process.env.DB_RESET === 'true') {
            logger.warn('⚠️ DB_RESET is true: Dropping and recreating all tables to fix migration errors...');
            await sequelize.sync({ force: true }); // This fixes the "USING" error by starting fresh
        } else if (process.env.DB_SYNC === 'true') {
            await sequelize.sync({ alter: true });
        }
        
    } catch (error) {
        logger.error('❌ Database connection failed:', error);
    }
};

module.exports = { sequelize, connectDB };
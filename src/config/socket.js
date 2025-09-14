const logger = require('./logger');

// Simple Socket.IO configuration
module.exports = (io) => {
    // Basic authentication middleware for Socket.IO
    io.use(async (socket, next) => {
        try {
            // For now, allow all connections - in production, add JWT verification
            socket.userId = 'anonymous';
            logger.debug('Socket.IO client connecting', {
                socketId: socket.id,
                handshake: socket.handshake.headers
            });
            next();
        } catch (error) {
            logger.error('Socket.IO authentication failed', {
                error: error.message,
                socketId: socket.id
            });
            next(new Error('Authentication failed'));
        }
    });

    // Connection event handler
    io.on('connection', (socket) => {
        logger.info('Socket.IO client connected', {
            socketId: socket.id,
            userId: socket.userId
        });

        // Handle basic ping/pong for connection health
        socket.on('ping', (callback) => {
            if (typeof callback === 'function') {
                callback('pong');
            }
        });

        // Handle disconnect
        socket.on('disconnect', (reason) => {
            logger.info('Socket.IO client disconnected', {
                socketId: socket.id,
                userId: socket.userId,
                reason
            });
        });

        // Handle errors
        socket.on('error', (error) => {
            logger.error('Socket.IO error', {
                socketId: socket.id,
                userId: socket.userId,
                error: error.message
            });
        });

        // Send welcome message
        socket.emit('connected', {
            message: 'Connected to Cloud Backup Server',
            socketId: socket.id,
            timestamp: new Date().toISOString()
        });
    });

    // Add utility methods to io object
    io.notifyUser = (userId, event, data) => {
        logger.debug('Socket.IO notification', { userId, event, data });
        // For now, broadcast to all connected clients
        // In production, this would target specific user rooms
        io.emit(event, data);
    };

    io.notifyFileUpload = (userId, fileData) => {
        io.notifyUser(userId, 'file_uploaded', {
            type: 'FILE_UPLOADED',
            file: fileData,
            timestamp: new Date().toISOString()
        });
    };

    logger.info('Socket.IO server configured');
    return io;
};
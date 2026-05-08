FROM node:22-slim

WORKDIR /app

COPY services/nl2sql-demo/frontend/package*.json ./
RUN npm ci

COPY services/nl2sql-demo/frontend .

EXPOSE 5173

CMD ["npm", "run", "dev"]

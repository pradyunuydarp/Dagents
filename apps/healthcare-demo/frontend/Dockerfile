FROM node:22-slim

WORKDIR /app

COPY apps/healthcare-demo/frontend/package*.json ./
RUN npm install

COPY apps/healthcare-demo/frontend .

EXPOSE 5174

CMD ["npm", "run", "dev"]
